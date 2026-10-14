"""Quant-stats panel math. Pure functions over plain lists/arrays.

Alpha here = realized edge of the deployed strategy, three ways:
1. edge_stats: per-match R = pnl/collateral over SETTLED bot matches, t vs 0
   (unit = match -> match-clustered by construction; the project's accounting scar).
2. curve stats: Sharpe/Sortino/maxDD/CAGR from the equity time series.
3. tracking: cumulative live mean R vs a backtest benchmark, with a 95% band that
   shrinks as 1/sqrt(n) as settled matches accumulate.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# A spread below this is floating-point noise, not variance: summing identical values can leave
# sd ~ 1e-17, which would otherwise turn into a t-stat or Sharpe of ~1e16.
_SD_EPS = 1e-12


# ---- funnel (bot activity counts) ----
def funnel(quote_rows, nfills, settlements):
    """Order/fill/volume counters. quote_rows = bot log quote events (order placements,
    incl. re-quotes); nfills = exchange fills (normalized); settlements = exchange rows."""
    placed = len(quote_rows)
    n_fills = len(nfills)
    contracts = sum(f["n"] for f in nfills)
    collateral = sum(f["cost"] for f in nfills)
    result_by_ticker = {str(s.get("ticker")): s.get("market_result")
                        for s in settlements or []}
    graded = [f for f in nfills if result_by_ticker.get(f["ticker"]) in ("yes", "no")]
    wins = sum(1 for f in graded if f["bought"] == result_by_ticker[f["ticker"]])
    return {"orders_placed": placed, "fills": n_fills,
            "fill_rate": round(n_fills / placed, 3) if placed else None,
            "volume_contracts": round(contracts, 2),
            "volume_collateral": round(collateral, 2),
            "settled_fills": len(graded),
            "fill_win_rate": round(wins / len(graded), 3) if graded else None}


# ---- edge stats ----
def _strat_ok(row, strategy):
    """strategy: None (all) | str | tuple/set of strs (e.g. combined = both bots)."""
    if strategy is None:
        return True
    if isinstance(strategy, str):
        return row["strategy"] == strategy
    return row["strategy"] in strategy


def edge_stats(match_rows, strategy=None):
    """Per-match ROI over settled matches of one strategy (or several). t is vs 0."""
    R = [r["roi"] for r in match_rows
         if r["status"] == "settled" and r["roi"] is not None
         and _strat_ok(r, strategy)]
    n = len(R)
    if n == 0:
        return {"n": 0, "roi_mean": None, "roi_sd": None, "t": None,
                "cents_per_contract": None, "win_rate": None}
    mean = sum(R) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in R) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if n > 1 and sd > _SD_EPS else None
    pnl = sum(r["pnl"] for r in match_rows
              if r["status"] == "settled" and r["pnl"] is not None
              and _strat_ok(r, strategy))
    contracts = sum(r["contracts"] for r in match_rows
                    if r["status"] == "settled" and _strat_ok(r, strategy))
    return {"n": n, "roi_mean": round(mean, 4), "roi_sd": round(sd, 4),
            "t": round(t, 2) if t is not None else None,
            "cents_per_contract": round(pnl / contracts * 100, 2) if contracts else None,
            "win_rate": round(sum(1 for x in R if x > 0) / n, 3)}


# ---- equity-curve stats ----
def daily_curve(points):
    """[(ts, equity)] -> [(day_start_ts, last_equity_of_day)]."""
    by_day = {}
    for ts, eq in points:
        by_day[ts // 86400] = eq
    return [(d * 86400, by_day[d]) for d in sorted(by_day)]


def daily_returns(points):
    days = daily_curve(points)
    out = []
    for i in range(1, len(days)):
        prev = days[i - 1][1]
        if prev > 0:
            out.append(days[i][1] / prev - 1.0)
    return out


def _mean_sd(xs):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return m, sd


def sharpe(returns, periods=365):
    m, sd = _mean_sd(returns)
    if sd <= _SD_EPS or len(returns) < 2:
        return None
    return round(m / sd * math.sqrt(periods), 2)


def sortino(returns, periods=365):
    if len(returns) < 2:
        return None
    m, _ = _mean_sd(returns)
    downs = [min(r, 0.0) for r in returns]
    dd = math.sqrt(sum(d * d for d in downs) / len(returns))
    if dd <= _SD_EPS:
        return None
    return round(m / dd * math.sqrt(periods), 2)


def max_drawdown(points):
    """-> (dd_frac, peak_ts, trough_ts) on the equity series."""
    peak, peak_ts = -1e18, None
    dd, span = 0.0, (None, None)
    for ts, eq in points:
        if eq > peak:
            peak, peak_ts = eq, ts
        elif peak > 0:
            d = 1 - eq / peak
            if d > dd:
                dd, span = d, (peak_ts, ts)
    return round(dd, 4), span[0], span[1]


def cagr(points):
    """Annualized growth; None when the window is under 7 days (report 'n/a: <7d')."""
    if len(points) < 2:
        return None
    t0, e0 = points[0]
    t1, e1 = points[-1]
    days = (t1 - t0) / 86400
    if days < 7 or e0 <= 0 or e1 <= 0:
        return None
    return round((e1 / e0) ** (365.0 / days) - 1.0, 4)


def curve_stats(points):
    rets = daily_returns(points)
    dd, p_ts, t_ts = max_drawdown(points)
    days = (points[-1][0] - points[0][0]) / 86400 if len(points) > 1 else 0
    return {"sharpe": sharpe(rets), "sortino": sortino(rets),
            "max_dd": dd, "dd_from": p_ts, "dd_to": t_ts,
            "cagr": cagr(points), "window_days": round(days, 1),
            "n_days": len(rets) + 1 if points else 0}


# ---- backtest benchmark + live tracking ----
# policy_adjusted_benchmark(): strategy-specific logic omitted. It builds the `bench` dict
# ({"bench_mean", "bench_sd"}) that tracking() compares live results against.


def tracking(match_rows, bench, strategy=None):
    """Cumulative live mean ROI after each settled match + shrinking 95% band around the
    benchmark. -> {live: [[n, cum_mean]], band: [[n, lo, hi]], bench_mean}."""
    settled = sorted((r for r in match_rows if r["status"] == "settled"
                      and r["roi"] is not None and _strat_ok(r, strategy)),
                     key=lambda r: r["last_ts"])
    live, band = [], []
    cum = 0.0
    for i, r in enumerate(settled, 1):
        cum += r["roi"]
        live.append([i, round(cum / i, 4)])
        half = 1.96 * bench["bench_sd"] / math.sqrt(i)
        band.append([i, round(bench["bench_mean"] - half, 4),
                     round(bench["bench_mean"] + half, 4)])
    return {"live": live, "band": band, "bench_mean": bench["bench_mean"],
            "n": len(settled)}


def day_change(points, now=None):
    """Equity change since UTC midnight -> (abs, pct) or (None, None)."""
    if not points:
        return None, None
    now = now or points[-1][0]
    day0 = int(datetime.fromtimestamp(now, tz=timezone.utc)
               .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    base = None
    for ts, eq in points:
        if ts < day0:
            base = eq
        else:
            base = base if base is not None else eq
            break
    if base is None or base <= 0:
        base = points[0][1]
    cur = points[-1][1]
    return round(cur - base, 2), round((cur / base - 1) * 100, 2) if base > 0 else None
