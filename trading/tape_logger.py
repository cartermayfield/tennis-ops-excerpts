"""FORWARD TRADE-TAPE + SETTLEMENT LOGGER — detached, read-only, self-terminating collector.

Purpose: grow the research sample FORWARD. It accumulates the public trade tape (taker_side + size +
price + ts) for every open market in the configured series, plus each market's authoritative
settlement, as one JSON file per ticker. An offline research gate can then be re-run on
history+forward data on a cadence (`run --gate-every N`).

Sibling of the other forward-testing collectors (order-book depth, live model-vs-market). PUBLIC
read-only endpoints, no auth, NO ORDERS. Hard self-termination after MAX_HOURS or MAX_MB.
One-instance pid guard.

Run detached (Windows):
  powershell Start-Process -WindowStyle Hidden python -ArgumentList "-m","trading.tape_logger"
CLI:
  python -m trading.tape_logger run          # long-running collector (resumes/append; Ctrl-C ok)
  python -m trading.tape_logger status       # tickers/trades/settlements captured so far
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROCESSED = Path(__file__).resolve().parents[1] / "data" / "processed"   # git-ignored data dir

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
HDR = {"User-Agent": "research-collector/0.1"}
OUT = PROCESSED / "tape_live"             # forward tape: one {ticker}.json per market
SETTLE = OUT / "settle.csv"               # authoritative ticker -> result (yes/no), one row per ticker
PID = OUT / "tape_logger.pid"
MAX_HOURS = 96
MAX_MB = 800
SERIES = ("SERIES_TICKER_1", "SERIES_TICKER_2")   # market series to collect (specific series omitted)
SWEEP_S = 30
MAX_TICKERS = 60
PAGE_PAUSE = 0.12
SETTLE_RECHECK_S = 180        # don't re-poll a not-yet-finalized finished market more often than this
MAX_SETTLE_PER_SWEEP = 30     # cap settlement lookups per sweep


def _markets(series, status):
    out, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": status, "limit": 200}
        if cursor:
            p["cursor"] = cursor
        try:
            r = requests.get(f"{BASE_URL}/markets", params=p, headers=HDR, timeout=15)
            if r.status_code != 200:
                break
            d = r.json()
        except Exception:                 # network error or non-JSON body: keep what we have
            break
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
        time.sleep(PAGE_PAUSE)
    return out


def active_tickers():
    out = []
    for s in SERIES:
        out += [m["ticker"] for m in _markets(s, "open")]
    return out[:MAX_TICKERS]


def new_trades(ticker, min_ts):
    """All trades for `ticker` with ts > min_ts (cursor-paginated), or None if any page fails.
    All-or-nothing: the tape pages newest-first, so keeping a partial result would advance the
    high-water mark past the trades on the failed pages and they would never be fetched."""
    out, cursor = [], None
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if min_ts:
            p["min_ts"] = min_ts
        if cursor:
            p["cursor"] = cursor
        try:
            r = requests.get(f"{BASE_URL}/markets/trades", params=p, headers=HDR, timeout=30)
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception:
            return None
        out += d.get("trades", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"):
            return out
        time.sleep(PAGE_PAUSE)


def _load_ticker(ticker):
    fn = OUT / f"{ticker}.json"
    if not fn.exists():
        return [], set(), 0
    try:
        rows = json.loads(fn.read_text())
    except Exception:
        return [], set(), 0
    ids = {r.get("trade_id") for r in rows}
    mx = max((_ts(r) for r in rows), default=0)
    return rows, ids, mx


def _write_rows(fn, rows):
    """Atomic replace. A crash mid-write can't leave a truncated file, which would load as empty
    and get overwritten by the next sweep, losing that ticker's history."""
    tmp = fn.with_name(fn.name + ".tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, fn)


def _ts(rec):
    t = rec.get("created_time")
    if not t:
        return 0
    try:
        return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _load_settled():
    done = {}
    if SETTLE.exists():
        with open(SETTLE, newline="") as fh:
            for row in csv.DictReader(fh):
                done[row["ticker"]] = row["result"]
    return done


def _market_result(ticker):
    """Targeted settlement lookup for ONE market. Kalshi's /markets `status` filter vocabulary is
    'settled' (NOT 'finalized' → that 400s); a finished market's object carries status=='finalized'
    with result in {'yes','no'}. Return that result, else None (not finalized yet)."""
    try:
        r = requests.get(f"{BASE_URL}/markets/{ticker}", headers=HDR, timeout=15)
        if r.status_code != 200:
            return None
        m = (r.json().get("market") or {})
    except Exception:
        return None
    if m.get("status") == "finalized" and m.get("result") in ("yes", "no"):
        return m["result"]
    return None


def _record_settlements(settled, collected, open_now, checked):
    """Record authoritative results for matches we've COLLECTED that have FINISHED (left the open set)
    but aren't settled yet. Targeted per-ticker + throttled (don't re-poll an unfinalized match every
    sweep). Finalization can lag the last point by minutes, so candidates are retried until they settle."""
    now = time.time()
    cand = [t for t in collected
            if t not in settled and t not in open_now and now - checked.get(t, 0) > SETTLE_RECHECK_S]
    cand = cand[:MAX_SETTLE_PER_SWEEP]
    if not cand:
        return 0
    added = 0
    new = not SETTLE.exists()
    with open(SETTLE, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ticker", "result", "recorded_ts"])
        for tk in cand:
            checked[tk] = now
            res = _market_result(tk)
            if res:
                w.writerow([tk, res, int(now)])
                settled[tk] = res
                added += 1
            time.sleep(0.1)
    return added


def _dir_mb():
    return sum(f.stat().st_size for f in OUT.glob("*.json")) / 1e6


def _run_gate():
    """Re-run the research deploy gate on history+forward and print GO/HOLD. Lazy import (heavy deps)."""
    try:
        print("\n" + "=" * 70 + f"\nGATE @ {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z\n" + "=" * 70)
        # strategy-specific logic omitted (private research gate is lazily imported and run here)
        print("=" * 70 + "\n", flush=True)
    except Exception as e:
        print(f"  [gate skipped: {type(e).__name__}: {e}]", flush=True)


def _pid_alive(pid):
    """True/False if we can determine liveness, None if unknown. Windows-safe (os.kill(pid,0) is NOT a
    liveness check on Windows — it sends CTRL_C_EVENT), so use tasklist there."""
    if not pid:
        return None
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:              # EPERM: the process exists, it just isn't ours
        return True
    except OSError:
        return False
    except Exception:
        return None


def _release_pid():
    """Remove the pid file on exit, but only if it is still ours. A leftover file whose pid the OS
    later reuses would otherwise look 'alive' and block every future start."""
    try:
        if PID.read_text().strip() == str(os.getpid()):
            PID.unlink()
    except Exception:
        pass


def run(gate_every_hours=0.0):
    OUT.mkdir(parents=True, exist_ok=True)
    if PID.exists():
        try:
            old = int(PID.read_text().strip())
        except Exception:
            old = None
        alive = _pid_alive(old)                       # True / False / None(unknown)
        # refuse only if the recorded pid is actually running, or liveness is unknown AND heartbeat fresh
        if alive is True or (alive is None and time.time() - PID.stat().st_mtime < 300):
            print(f"another tape_logger appears live (pid {old}) — exiting")
            return
        if alive is False:
            print(f"clearing stale pid file (pid {old} not running)")
    t0 = time.time()
    settled = _load_settled()
    seen_max = {}                      # ticker -> max ts captured (in-memory cache across sweeps)
    checked = {}                       # ticker -> last settlement-poll ts (throttle)
    n_new = 0
    t_gate = time.time()
    if gate_every_hours:
        print(f"combined mode: collect + re-gate every {gate_every_hours:g}h")
    print(f"forward tape -> {OUT} every {SWEEP_S}s (public, read-only, no orders). Ctrl-C to stop.")
    try:
        while True:
            if time.time() - t0 > MAX_HOURS * 3600 or _dir_mb() > MAX_MB:
                break
            if gate_every_hours and time.time() - t_gate >= gate_every_hours * 3600:
                _run_gate()
                t_gate = time.time()
            PID.write_text(str(os.getpid()))           # heartbeat (mtime) + single-instance claim
            ticks = active_tickers()
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            for tk in ticks:
                if tk not in seen_max:
                    rows, ids, mx = _load_ticker(tk)
                    seen_max[tk] = (rows, ids, mx)
                rows, ids, mx = seen_max[tk]
                got = new_trades(tk, mx - 1 if mx else 0)  # 1 s overlap; trade_id dedup drops repeats
                fresh = [t for t in got or [] if t.get("trade_id") not in ids]   # None: retry next sweep
                if fresh:
                    for t in fresh:
                        ids.add(t.get("trade_id"))
                    rows += fresh
                    mx = max(mx, max(_ts(t) for t in fresh))
                    seen_max[tk] = (rows, ids, mx)
                    _write_rows(OUT / f"{tk}.json", rows)
                    n_new += len(fresh)
                time.sleep(0.15)
            collected = {f.stem for f in OUT.glob("*.json")}      # every ticker we hold tape for
            s_add = _record_settlements(settled, collected, set(ticks), checked)
            print(f"  {now_str}  {len(ticks)} open | +{n_new} trades cum | settled {len(settled)} "
                  f"(+{s_add}) | {_dir_mb():.0f}MB", flush=True)
            time.sleep(max(5, SWEEP_S - 0.15 * len(ticks)))
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        _release_pid()
    print(f"tape_logger done: {n_new} new trades captured across {len(seen_max)} tickers; "
          f"{len(settled)} settlements.")


def status():
    files = list(OUT.glob("*.json"))
    tot = 0
    for f in files:
        try:
            tot += len(json.loads(f.read_text()))
        except Exception:
            pass
    settled = _load_settled()
    print(f"forward tape dir: {OUT}")
    print(f"  tickers: {len(files)}   trades: {tot:,}   settlements: {len(settled)}   size: {_dir_mb():.0f}MB")
    if files:
        newest = max(files, key=lambda p: p.stat().st_mtime)
        print(f"  most recent file: {newest.name} @ {datetime.fromtimestamp(newest.stat().st_mtime):%Y-%m-%d %H:%M}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forward-collect the public trade tape + settlement.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="collect forward (add --gate-every to also print the deploy gate on a cadence)")
    r.add_argument("--gate-every", type=float, default=0.0, metavar="HOURS",
                   help="also re-run the research GO/HOLD gate every N hours (0=collect only)")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        status()
    else:
        run(getattr(a, "gate_every", 0.0))


if __name__ == "__main__":
    sys.exit(main())
