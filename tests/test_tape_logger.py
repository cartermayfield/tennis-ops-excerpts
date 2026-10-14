"""trading/tape_logger.py against a fake exchange: no live HTTP, no real sleeps, output in tmp_path.

One opt-in smoke test at the bottom hits Kalshi's public, unauthenticated market-data endpoints
(read-only, writes nothing). It is skipped unless KALSHI_LIVE_SMOKE=1.
"""
import json
import os
import time as real_time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from trading import tape_logger as tl


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trade(tid: str, ts: int) -> dict:
    return {"trade_id": tid, "created_time": iso(ts), "taker_side": "yes", "count": 1,
            "yes_price": 50}


class FakeResp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class FakeExchange:
    """The three public endpoints tape_logger reads, with cursor paging and failure injection."""

    def __init__(self):
        self.open = {}            # series -> [tickers]
        self.trades = {}          # ticker -> [trade dicts]
        self.status = {}          # ticker -> market dict for /markets/{ticker}
        self.page = 1000          # page size override (to force pagination)
        self.fail = {}            # (path, page_no) -> FakeResp to return instead
        self.calls = []

    def _paged(self, path, items, key, params):
        start = int(params.get("cursor") or 0)
        page_no = start // self.page
        if (path, page_no) in self.fail:
            return self.fail[(path, page_no)]
        chunk = items[start:start + self.page]
        nxt = start + self.page
        return FakeResp(200, {key: chunk, "cursor": str(nxt) if nxt < len(items) else ""})

    def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        path = url.removeprefix(tl.BASE_URL)
        self.calls.append((path, params))
        if path == "/markets":
            assert params["status"] == "open"
            mk = [{"ticker": t} for t in self.open.get(params["series_ticker"], [])]
            return self._paged(path, mk, "markets", params)
        if path == "/markets/trades":
            min_ts = params.get("min_ts", 0)                 # exclusive, like the docstring says
            rows = sorted((t for t in self.trades.get(params["ticker"], [])
                           if tl._ts(t) > min_ts), key=tl._ts, reverse=True)   # newest first
            return self._paged(path, rows, "trades", params)
        tk = path.removeprefix("/markets/")
        return FakeResp(200, {"market": self.status.get(tk, {"ticker": tk, "status": "active"})})

    def trade_calls(self, ticker):
        return [p for path, p in self.calls if path == "/markets/trades" and p["ticker"] == ticker]


class FakeClock:
    """Replaces tape_logger's `time` module: sleep() advances the clock instead of waiting."""

    def __init__(self):
        self.now = real_time.time()

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def ex(tmp_path, monkeypatch):
    fake = FakeExchange()
    out = tmp_path / "tape_live"
    monkeypatch.setattr(tl, "OUT", out)
    monkeypatch.setattr(tl, "SETTLE", out / "settle.csv")
    monkeypatch.setattr(tl, "PID", out / "tape_logger.pid")
    monkeypatch.setattr(tl, "SERIES", ("SERIES_A", "SERIES_B"))
    monkeypatch.setattr(tl, "MAX_HOURS", 10 / 3600)        # stop after one sweep (sweep sleeps >= 5 s)
    monkeypatch.setattr(tl, "requests", SimpleNamespace(get=fake.get))
    monkeypatch.setattr(tl, "time", FakeClock())
    return fake


def read_rows(ticker):
    return json.loads((tl.OUT / f"{ticker}.json").read_text())


# ---- parsing ----
def test_ts_parses_iso_and_fails_soft():
    want = int(datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    assert tl._ts({"created_time": "2026-07-01T12:00:00Z"}) == want
    assert tl._ts({"created_time": "2026-07-01T12:00:00.123456Z"}) == want
    assert tl._ts({"created_time": "2026-07-01T12:00:00.12Z"}) == want
    assert tl._ts({}) == 0
    assert tl._ts({"created_time": "not a time"}) == 0


# ---- market discovery ----
def test_active_tickers_paginates_each_series_and_caps(ex, monkeypatch):
    ex.page = 2
    ex.open = {"SERIES_A": ["A1", "A2", "A3"], "SERIES_B": ["B1"]}
    assert tl.active_tickers() == ["A1", "A2", "A3", "B1"]
    assert [p.get("cursor") for path, p in ex.calls] == [None, "2", None]
    monkeypatch.setattr(tl, "MAX_TICKERS", 3)
    assert tl.active_tickers() == ["A1", "A2", "A3"]


def test_market_listing_error_keeps_what_it_has(ex):
    ex.page = 2
    ex.open = {"SERIES_A": ["A1", "A2", "A3"]}
    ex.fail[("/markets", 1)] = FakeResp(500, text="oops")
    # benign: a short open list only means fewer tickers this sweep (settlement needs "finalized")
    assert [m["ticker"] for m in tl._markets("SERIES_A", "open")] == ["A1", "A2"]


def test_market_listing_survives_non_json_body(ex):
    ex.open = {"SERIES_A": ["A1"]}
    ex.fail[("/markets", 0)] = FakeResp(200, text="<html>gateway</html>")
    assert tl._markets("SERIES_A", "open") == []


# ---- trade tape ----
def test_new_trades_paginates(ex):
    ex.page = 2
    ex.trades["T"] = [trade(f"t{i}", 1000 + i) for i in range(5)]
    got = tl.new_trades("T", 0)
    assert sorted(t["trade_id"] for t in got) == ["t0", "t1", "t2", "t3", "t4"]
    assert len(ex.trade_calls("T")) == 3


def test_new_trades_is_all_or_nothing(ex):
    ex.page = 2
    ex.trades["T"] = [trade(f"t{i}", 1000 + i) for i in range(5)]
    ex.fail[("/markets/trades", 1)] = FakeResp(503, text="busy")
    assert tl.new_trades("T", 0) is None                   # not the newest page alone
    ex.fail = {("/markets/trades", 0): FakeResp(200, text="not json")}
    assert tl.new_trades("T", 0) is None


def test_sweep_appends_dedups_and_resumes_after_restart(ex):
    tl.OUT.mkdir(parents=True)
    (tl.OUT / "T-A.json").write_text(json.dumps([trade("a1", 100), trade("a2", 200)]))
    ex.open = {"SERIES_A": ["T-A"], "SERIES_B": ["T-B"]}
    ex.trades = {"T-A": [trade("a1", 100), trade("a2", 200), trade("a3", 300)],
                 "T-B": [trade("b1", 250)]}
    tl.run()
    assert [r["trade_id"] for r in read_rows("T-A")] == ["a1", "a2", "a3"]   # appended once
    assert [r["trade_id"] for r in read_rows("T-B")] == ["b1"]
    assert ex.trade_calls("T-A")[0]["min_ts"] == 199         # resumed from the file's newest ts
    assert "min_ts" not in ex.trade_calls("T-B")[0]          # new ticker: full history
    assert not list(tl.OUT.glob("*.tmp"))                    # atomic writes leave no temp files

    # restart; a late trade landed in the same second as the last one we saved
    ex.trades["T-A"].append(trade("a4", 300))
    ex.calls.clear()
    tl.run()
    assert [r["trade_id"] for r in read_rows("T-A")] == ["a1", "a2", "a3", "a4"]
    assert ex.trade_calls("T-A")[0]["min_ts"] == 299         # 1 s overlap catches a4


def test_failed_page_does_not_advance_high_water_mark(ex):
    ex.page = 1
    ex.open = {"SERIES_A": ["T-A"]}
    ex.trades = {"T-A": [trade("old", 100), trade("new", 200)]}
    ex.fail[("/markets/trades", 1)] = FakeResp(500, text="err")   # newest page ok, older page fails
    tl.run()
    assert not (tl.OUT / "T-A.json").exists()                # nothing recorded past a gap
    ex.fail.clear()
    tl.run()
    assert sorted(r["trade_id"] for r in read_rows("T-A")) == ["new", "old"]


def test_atomic_write_keeps_old_file_if_replace_fails(tmp_path, monkeypatch):
    fn = tmp_path / "T.json"
    tl._write_rows(fn, [trade("a1", 100)])

    def crash(*_):
        raise OSError("simulated crash mid-write")
    monkeypatch.setattr(tl, "os", SimpleNamespace(replace=crash))
    with pytest.raises(OSError):
        tl._write_rows(fn, [trade("a1", 100), trade("a2", 200)])
    assert [r["trade_id"] for r in json.loads(fn.read_text())] == ["a1"]


def test_load_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "OUT", tmp_path)
    (tmp_path / "T.json").write_text(json.dumps([trade("a1", 100), trade("a2", 250)]))
    rows, ids, mx = tl._load_ticker("T")
    assert len(rows) == 2 and ids == {"a1", "a2"} and mx == 250
    assert tl._load_ticker("MISSING") == ([], set(), 0)


# ---- settlements ----
def test_settlements_recorded_throttled_and_capped(ex):
    tl.OUT.mkdir(parents=True)
    ex.status = {"S-1": {"status": "finalized", "result": "yes"},
                 "S-2": {"status": "closed", "result": ""},         # finished, not final yet
                 "S-3": {"status": "finalized", "result": ""}}      # final but no result: skip
    settled, checked = {}, {}
    collected = {"S-1", "S-2", "S-3", "OPEN-1"}
    assert tl._record_settlements(settled, collected, {"OPEN-1"}, checked) == 1
    assert settled == {"S-1": "yes"}
    looked_up = {path for path, _ in ex.calls}
    assert looked_up == {"/markets/S-1", "/markets/S-2", "/markets/S-3"}  # never the open one

    ex.calls.clear()                                       # immediately again: throttled
    assert tl._record_settlements(settled, collected, {"OPEN-1"}, checked) == 0
    assert ex.calls == []

    tl.time.sleep(tl.SETTLE_RECHECK_S + 1)                 # later, S-2 finalizes
    ex.status["S-2"] = {"status": "finalized", "result": "no"}
    assert tl._record_settlements(settled, collected, {"OPEN-1"}, checked) == 1
    assert tl._load_settled() == {"S-1": "yes", "S-2": "no"}   # round-trips through the CSV
    assert tl.SETTLE.read_text().splitlines()[0] == "ticker,result,recorded_ts"

    ex.calls.clear()
    many = {f"M-{i}" for i in range(tl.MAX_SETTLE_PER_SWEEP + 5)}
    tl._record_settlements({}, many, set(), {})
    assert len(ex.calls) == tl.MAX_SETTLE_PER_SWEEP


# ---- single-instance guard ----
def write_pid(pid_text, age_s=0):
    tl.OUT.mkdir(parents=True, exist_ok=True)
    tl.PID.write_text(pid_text)
    t = real_time.time() - age_s
    os.utime(tl.PID, (t, t))


def test_refuses_to_start_when_recorded_pid_is_alive(ex, monkeypatch, capsys):
    write_pid("4242")
    monkeypatch.setattr(tl, "_pid_alive", lambda pid: True)
    tl.run()
    assert "appears live (pid 4242)" in capsys.readouterr().out
    assert tl.PID.read_text() == "4242" and ex.calls == []    # untouched, no API traffic


@pytest.mark.parametrize("pid_text", ["4242", "garbage"])
def test_fails_closed_when_liveness_unknown_and_heartbeat_fresh(ex, monkeypatch, capsys, pid_text):
    write_pid(pid_text, age_s=10)
    monkeypatch.setattr(tl, "_pid_alive", lambda pid: None)
    tl.run()
    assert "appears live" in capsys.readouterr().out and ex.calls == []


def test_starts_when_liveness_unknown_but_heartbeat_stale(ex, monkeypatch, capsys):
    write_pid("4242", age_s=600)
    monkeypatch.setattr(tl, "_pid_alive", lambda pid: None)
    tl.run()
    assert "tape_logger done" in capsys.readouterr().out and ex.calls != []


def test_clears_stale_pid_and_removes_own_pid_on_exit(ex, monkeypatch, capsys):
    write_pid("4242")
    monkeypatch.setattr(tl, "_pid_alive", lambda pid: False)
    tl.run()
    out = capsys.readouterr().out
    assert "clearing stale pid file (pid 4242 not running)" in out
    assert "tape_logger done" in out
    assert not tl.PID.exists()                              # we cleaned up after ourselves


def test_release_pid_leaves_another_instances_file(ex):
    write_pid("999999")
    tl._release_pid()
    assert tl.PID.read_text() == "999999"
    write_pid(str(os.getpid()))
    tl._release_pid()
    assert not tl.PID.exists()


def test_ctrl_c_exits_cleanly(ex, monkeypatch, capsys):
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(tl, "active_tickers", interrupt)
    tl.run()
    out = capsys.readouterr().out
    assert "interrupted" in out and "tape_logger done" in out
    assert not tl.PID.exists()


def test_stops_itself_at_size_cap(ex, monkeypatch, capsys):
    monkeypatch.setattr(tl, "MAX_HOURS", 96)
    monkeypatch.setattr(tl, "MAX_MB", -1)                   # already "over" the cap
    tl.run()
    assert "tape_logger done" in capsys.readouterr().out and ex.calls == []


# ---- pid liveness probe ----
def fake_tasklist(stdout=None, exc=None):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        if exc:
            raise exc
        return SimpleNamespace(stdout=stdout)
    return SimpleNamespace(run=run), seen


def test_pid_alive_windows_uses_tasklist(monkeypatch):
    monkeypatch.setattr(tl, "os", SimpleNamespace(name="nt"))
    sp, seen = fake_tasklist("python.exe   4242 Console   1   20,000 K\n")
    monkeypatch.setattr(tl, "subprocess", sp)
    assert tl._pid_alive(4242) is True
    assert seen[0] == ["tasklist", "/FI", "PID eq 4242", "/NH"]
    sp, _ = fake_tasklist("INFO: No tasks are running which match the specified criteria.\n")
    monkeypatch.setattr(tl, "subprocess", sp)
    assert tl._pid_alive(4242) is False
    sp, _ = fake_tasklist(exc=FileNotFoundError("tasklist"))
    monkeypatch.setattr(tl, "subprocess", sp)
    assert tl._pid_alive(4242) is None                      # unknown, not "dead"


@pytest.mark.parametrize("exc, want", [(None, True), (ProcessLookupError(), False),
                                       (PermissionError(), True)])
def test_pid_alive_posix_signal_zero(monkeypatch, exc, want):
    def kill(pid, sig):
        assert sig == 0
        if exc:
            raise exc
    monkeypatch.setattr(tl, "os", SimpleNamespace(name="posix", kill=kill))
    assert tl._pid_alive(4242) is want


def test_pid_alive_unknown_without_pid():
    assert tl._pid_alive(None) is None


# ---- status ----
def test_status_summarises_output(ex, capsys):
    tl.OUT.mkdir(parents=True)
    tl._write_rows(tl.OUT / "T-A.json", [trade("a1", 1), trade("a2", 2), trade("a3", 3)])
    tl._write_rows(tl.OUT / "T-B.json", [trade("b1", 1), trade("b2", 2)])
    tl.SETTLE.write_text("ticker,result,recorded_ts\nT-A,yes,1\n")
    tl.status()
    assert "tickers: 2   trades: 5   settlements: 1" in capsys.readouterr().out


def test_status_before_first_run(ex, capsys):
    tl.status()                                            # no output dir yet: must not crash
    assert "tickers: 0" in capsys.readouterr().out


# ---- opt-in live smoke test (public, read-only) ----
@pytest.mark.skipif(os.environ.get("KALSHI_LIVE_SMOKE") != "1",
                    reason="live API; set KALSHI_LIVE_SMOKE=1 to run (public, read-only)")
def test_live_public_endpoints_smoke():
    r = requests.get(f"{tl.BASE_URL}/markets", params={"status": "open", "limit": 1},
                     headers=tl.HDR, timeout=15)
    assert r.status_code == 200
    tk = r.json()["markets"][0]["ticker"]
    trades = tl.new_trades(tk, int(real_time.time()) - 3600)
    assert trades is not None
    assert all(t.get("trade_id") and tl._ts(t) > 0 for t in trades)
    assert tl._market_result(tk) is None                    # an open market has no result yet
