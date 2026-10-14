"""portfolio/stats.py against hand-computed values. The arithmetic is in the comments so each
expected number can be checked with a pencil, not by re-running the formula under test."""
import pytest

from portfolio import stats


def mrow(roi, *, pnl=None, strategy="strat_a", status="settled", contracts=5, ts=0):
    return {"roi": roi, "pnl": pnl, "strategy": strategy, "status": status,
            "contracts": contracts, "last_ts": ts}


# ---- funnel ----
def test_funnel_counts_and_fill_win_rate():
    quotes = [{"event": "quote"}] * 10
    nfills = [{"n": 5.0, "cost": 3.9, "ticker": "T-A", "bought": "no"},
              {"n": 2.0, "cost": 1.5, "ticker": "T-B", "bought": "yes"},
              {"n": 1.0, "cost": 0.5, "ticker": "T-C", "bought": "yes"}]
    settles = [{"ticker": "T-A", "market_result": "no"},      # win
               {"ticker": "T-B", "market_result": "no"}]      # loss; T-C not settled yet
    f = stats.funnel(quotes, nfills, settles)
    assert f == {"orders_placed": 10, "fills": 3, "fill_rate": 0.3,   # 3 / 10
                 "volume_contracts": 8.0, "volume_collateral": 5.9,
                 "settled_fills": 2, "fill_win_rate": 0.5}


def test_funnel_empty():
    f = stats.funnel([], [], None)
    assert f["orders_placed"] == 0 and f["fills"] == 0
    assert f["fill_rate"] is None and f["fill_win_rate"] is None


# ---- edge stats (match is the unit) ----
def test_edge_stats_hand_computed_t():
    # R = .10, -.05, .20, .15  ->  mean = .40 / 4 = .10
    # squared deviations: 0, .0225, .01, .0025 -> sum .035 -> var = .035 / 3 = .011667
    # sd = .10801 ; se = sd / sqrt(4) = .054006 ; t = .10 / .054006 = 1.85
    rows = [mrow(.10, pnl=.50), mrow(-.05, pnl=-.25), mrow(.20, pnl=1.00), mrow(.15, pnl=.75)]
    e = stats.edge_stats(rows)
    assert e["n"] == 4
    assert e["roi_mean"] == 0.1
    assert e["roi_sd"] == 0.108
    assert e["t"] == 1.85
    assert e["win_rate"] == 0.75
    assert e["cents_per_contract"] == 10.0        # pnl 2.00 / 20 contracts = $0.10


def test_edge_stats_filters_strategy_and_unsettled():
    rows = [mrow(.10, pnl=.5), mrow(.20, pnl=1.0),
            mrow(9.9, pnl=9.9, strategy="other"),        # different strategy
            mrow(.50, pnl=2.0, status="open"),           # not settled yet
            mrow(None, pnl=0.0)]                         # settled but no ROI
    assert stats.edge_stats(rows, strategy="strat_a")["n"] == 2
    assert stats.edge_stats(rows, strategy=("strat_a", "other"))["n"] == 3
    assert stats.edge_stats(rows)["n"] == 3              # None = all strategies


def test_edge_stats_edge_cases():
    empty = stats.edge_stats([])
    assert empty == {"n": 0, "roi_mean": None, "roi_sd": None, "t": None,
                     "cents_per_contract": None, "win_rate": None}
    one = stats.edge_stats([mrow(.2, pnl=1.0)])
    assert one["n"] == 1 and one["roi_mean"] == 0.2 and one["roi_sd"] == 0.0
    assert one["t"] is None                               # no t from a single match
    flat = stats.edge_stats([mrow(.05, pnl=.25)] * 3)
    assert flat["roi_sd"] == 0.0 and flat["t"] is None   # zero variance: t undefined, not inf


# ---- equity-curve stats ----
RETS = [0.02, -0.01, 0.02, -0.01]
# mean = .005 ; deviations +-.015 -> var = 4 * .000225 / 3 = .0003 -> sd = .017321
# Sharpe (per period) = .005 / .017321 = .2887
# downside dev = sqrt((.01^2 + .01^2) / 4) = sqrt(.00005) = .0070711
# Sortino (per period) = .005 / .0070711 = .7071 (= 1/sqrt 2)
# annualised: x sqrt(365) = x 19.10497


def test_sharpe_hand_computed():
    assert stats.sharpe(RETS, periods=1) == 0.29
    assert stats.sharpe(RETS) == 5.52                     # .28868 * 19.10497 = 5.515


def test_sortino_hand_computed():
    assert stats.sortino(RETS, periods=1) == 0.71
    assert stats.sortino(RETS) == 13.51                   # .70711 * 19.10497 = 13.509


def test_sharpe_sortino_edge_cases():
    assert stats.sharpe([]) is None
    assert stats.sharpe([0.01]) is None                   # single sample
    assert stats.sharpe([0.01, 0.01, 0.01]) is None       # zero variance
    assert stats.sharpe([0.1, 0.1, 0.1]) is None          # zero variance + float noise (sd ~1e-17)
    assert stats.sortino([]) is None
    assert stats.sortino([0.01]) is None
    assert stats.sortino([0.01, 0.02]) is None            # no downside -> undefined


def test_max_drawdown_hand_computed():
    pts = [(0, 100.0), (1, 120.0), (2, 90.0), (3, 110.0), (4, 80.0), (5, 130.0)]
    # peak 120 at t=1; worst trough 80 at t=4 -> 1 - 80/120 = .3333 (the later 130 high
    # does not erase it)
    assert stats.max_drawdown(pts) == (0.3333, 1, 4)


def test_max_drawdown_edge_cases():
    assert stats.max_drawdown([]) == (0.0, None, None)
    assert stats.max_drawdown([(0, 5.0)]) == (0.0, None, None)
    assert stats.max_drawdown([(0, 1.0), (1, 2.0), (2, 3.0)]) == (0.0, None, None)
    assert stats.max_drawdown([(0, 2.0), (1, 2.0)]) == (0.0, None, None)


def test_cagr_and_short_window_guard():
    # 73 days = 1/5 year: 1.1^5 - 1 = .61051
    assert stats.cagr([(0, 100.0), (73 * 86400, 110.0)]) == 0.6105
    assert stats.cagr([(0, 100.0), (3 * 86400, 110.0)]) is None     # < 7 days: refuse
    assert stats.cagr([(0, 100.0)]) is None
    assert stats.cagr([]) is None


def test_daily_curve_and_returns():
    pts = [(10, 100.0), (86400 + 5, 110.0), (86400 + 99, 105.0), (2 * 86400 + 1, 210.0)]
    assert stats.daily_curve(pts) == [(0, 100.0), (86400, 105.0), (172800, 210.0)]  # last per day
    assert stats.daily_returns(pts) == pytest.approx([0.05, 1.0])                   # 105/100, 210/105


def test_curve_stats_empty_and_single_point():
    empty = stats.curve_stats([])
    assert empty["sharpe"] is None and empty["sortino"] is None and empty["cagr"] is None
    assert empty["max_dd"] == 0.0 and empty["window_days"] == 0 and empty["n_days"] == 0
    one = stats.curve_stats([(0, 100.0)])
    assert one["n_days"] == 1 and one["max_dd"] == 0.0 and one["sharpe"] is None


# ---- live-vs-backtest tracking band ----
def test_tracking_hand_computed_band():
    bench = {"bench_mean": 0.04, "bench_sd": 0.20}
    rows = [mrow(.20, ts=3), mrow(.10, ts=1), mrow(.15, ts=4), mrow(-.05, ts=2),
            mrow(.90, ts=5, status="open")]               # unsettled: excluded
    t = stats.tracking(rows, bench)
    # sorted by time: .10, -.05, .20, .15 -> running means .10, .025, .0833, .10
    assert t["live"] == [[1, 0.1], [2, 0.025], [3, 0.0833], [4, 0.1]]
    # half-width = 1.96 * .20 / sqrt(n): n=1 -> .392, n=2 -> .27719, n=4 -> .196
    assert t["band"][0] == [1, -0.352, 0.432]
    assert t["band"][1] == [2, -0.2372, 0.3172]
    assert t["band"][3] == [4, -0.156, 0.236]
    assert t["n"] == 4 and t["bench_mean"] == 0.04


def test_tracking_empty():
    t = stats.tracking([], {"bench_mean": 0.04, "bench_sd": 0.2})
    assert t == {"live": [], "band": [], "bench_mean": 0.04, "n": 0}


def test_day_change():
    day0 = 100 * 86400                                    # a UTC midnight
    pts = [(day0 - 100, 100.0), (day0 + 10, 101.0), (day0 + 20, 105.0)]
    assert stats.day_change(pts) == (5.0, 5.0)            # vs last value before midnight
    assert stats.day_change([(day0 + 10, 50.0), (day0 + 20, 55.0)]) == (5.0, 10.0)
    assert stats.day_change([]) == (None, None)
