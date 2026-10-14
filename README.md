# Tennis Ops: code sample

Three lightly edited excerpts from **Tennis Ops**, my production algorithmic trading system for live
in-play pro tennis markets on Kalshi and Polymarket US (June 2026 to present), with a test suite for each.

**About the system**
- Six autonomous trading bots, ~94,000 lines of Python, placing, repricing and canceling orders 24/7.
- $5,000+ realized profit (+112.3% NAV) across 2,200+ trades in under 2 months.
- 10+ collector daemons (two independent live-score feeds, order-book depth, trade tape, settlements)
  with fail-closed guards and staleness tripwires.
- 150+ preregistered research cycles using out-of-sample time splits, match-clustered standard errors,
  Benjamini–Hochberg correction, and fee-aware executable-fill backtests. 70+ strategies falsified.
  The flagship market-making strategy was validated out of sample at +12.8% stake-weighted ROI
  (128 bets, t = 5.0) before any capital went in.
- A live portfolio dashboard (P&L, NAV, clustered t-stats), daily automated audits, one-command restarts.
- Built primarily with Claude Code as the agentic harness. I owned the architecture, strategy
  preregistration, and validation.

**The full repository is private** because it trades live capital and the strategy is proprietary. These
files are infrastructure only. I removed strategy-specific config, market and segment names, internal
paths, and research hooks, and marked each cut with `# strategy-specific logic omitted` or
`(... omitted)`. I'm happy to walk through the full system in an interview.

---

## `trading/tape_logger.py`: forward data-collection daemon

A detached, read-only collector that records the public trade tape and settlement results for every
open market in a set of series.

- **Incremental and restart-safe.** Each ticker keeps a high-water-mark timestamp and a set of
  `trade_id`s, so a restart resumes and appends without duplicates. Every API call is cursor-paginated.
  A trade fetch is all-or-nothing and overlaps the previous second, so a failed page or a late trade in
  the same second can't leave a permanent gap. Files are replaced atomically, so a crash mid-write
  can't truncate a ticker's history.
- **Throttled settlement tracking.** When a market leaves the open set, the collector looks up its
  result one ticker at a time, rechecking each no more than every 180 s with at most 30 lookups per
  sweep, until the exchange finalizes it. It also handles an API gotcha: the `settled` filter
  vocabulary differs from the `finalized` status.
- **Self-limiting.** It stops on its own after `MAX_HOURS` or once its output passes `MAX_MB` on disk,
  so a forgotten background process can't run forever or fill a disk.
- **Single-instance guard with stale-PID detection.** It uses a PID file as a heartbeat. The liveness
  check works on Windows, where `os.kill(pid, 0)` sends CTRL_C_EVENT instead of probing, so the code
  calls `tasklist`. A PID file left by a crashed run is cleared, and a clean exit removes its own. When
  liveness can't be determined and the heartbeat is fresh, the collector refuses to start (it fails
  closed).
- **Isolated evaluation hook.** An optional periodic evaluation runs inside its own try/except, so a
  failing evaluation never takes down the collector.

## `trading/kalshi_client.py`: authenticated exchange client

- **Request signing.** Each request is signed with RSA-PSS / SHA-256 over `timestamp + METHOD + path`,
  covering the path only, without the query string.
- **Retry with capped exponential backoff.** HTTP 429 and 5xx responses are retried (2, 4, 8 s), and
  each attempt is signed with a fresh timestamp. Any other error, or running out of retries, raises
  `KalshiAPIError` with the status code and a truncated body.
- **Fail-closed writes.** `create_order` and `cancel_order` raise `PermissionError` unless called
  with `confirm=True`. A malformed order (unknown action, price outside 1–99¢, count below 1) is
  rejected before anything is signed. Orders default to post-only, so they are rejected rather than
  crossing the spread, and self-trade prevention is on.
- **Response normalization.** It reads both the old and new API response shapes: order books in
  cents or dollars, and positions as `position_fp` strings.
- **No secrets in code.** Credentials load from a git-ignored TOML file, or can be passed in directly
  (the tests use a throwaway key).

## `portfolio/stats.py`: dashboard statistics (pure functions, no I/O)

- **`edge_stats`** computes a t-statistic with the *match* as the unit of observation, so many
  correlated fills inside one match can't inflate significance (match-clustered by construction).
- **Equity-curve metrics.** It resamples equity to daily values and computes Sharpe, Sortino
  (downside deviation), max drawdown with peak and trough timestamps, and CAGR. CAGR refuses to
  annualize windows shorter than 7 days, and zero-variance input returns `None` instead of a
  meaningless t-stat or Sharpe.
- **`tracking`** plots the cumulative live mean return against the backtest benchmark inside a 95%
  band that shrinks as 1/√n. This gives a running check on whether live results are consistent
  with the backtest.
- **`funnel`** counts orders, fills, fill rate, volume, and the win rate of settled fills.

## Design notes

- **Fail closed.** When the code can't be sure, it stops instead of guessing. The client raises
  rather than returning an empty response, so an exchange outage can never read as "empty order book"
  or "no positions" to a bot. The collector won't start if another copy might be running. The stats
  return `None` rather than a number when the sample can't support one.
- **Staleness and PID guards.** The collector's PID file doubles as a heartbeat, rewritten every
  sweep. On startup there are three cases: the recorded process is running (exit), it is confirmed
  dead (clear the file and start), or liveness is unknown (start only if the heartbeat is more than
  5 minutes old). Hard caps on runtime and disk use mean nothing runs unattended forever.
- **Signed requests, retries, and explicit confirmation before orders.** Only transient errors are
  retried. The `client_order_id` is generated once per order, so a retried POST carries the same id.
  Nothing that moves money happens without `confirm=True`, and the bots pass that only after a person
  arms the session.
- **Match-clustered statistics.** Fills inside one tennis match share the same players and the same
  score path, so they are not independent. Counting each fill as an observation would overstate
  significance. `edge_stats` collapses each match to one return and computes the t-stat across
  matches, and the tracking band narrows with the number of settled matches, not fills.

## What I'd change next

- The single-instance guard reads the PID file and then writes it, so two copies started in the same
  instant could both pass. An exclusive create (`O_EXCL`) or an OS file lock would close that gap.
- Each ticker's tape is one JSON file, rewritten whenever new trades arrive. That's simple and
  crash-safe at this scale (the collector caps itself at 800 MB), but append-only JSON Lines or
  Parquet would scale better.
- The client retries HTTP 429/5xx but not timeouts or dropped connections. Retrying a read is easy.
  Retrying an order POST is only safe if the exchange dedupes on `client_order_id`, and I haven't
  verified that.

## Running the tests

```
python -m pip install -r requirements.txt
python -m pytest
```

No credentials and no network needed. The client tests generate a throwaway RSA key and check each
signature against its public key over the exact message Kalshi specifies. HTTP goes to fake sessions,
and the collector runs a full sweep against a fake exchange with a fake clock, so nothing actually
sleeps. Expected values in `tests/test_stats.py` are worked by hand in the comments.

One opt-in smoke test reads Kalshi's public market-data endpoints (no auth, read-only, writes nothing):

```
KALSHI_LIVE_SMOKE=1 python -m pytest -k smoke              # bash
$env:KALSHI_LIVE_SMOKE=1; python -m pytest -k smoke        # PowerShell
```

## Running the code

Requires Python 3.11+ (`tomllib`). `stats.py` has no dependencies.

- `tape_logger.py`: put real series tickers in `SERIES`, then `python -m trading.tape_logger run`
  (or `status`). Output goes to `data/processed/tape_live/`, which is git-ignored.
- `kalshi_client.py`: create `secrets.toml` at the repo root (git-ignored) with `kalshi_key_id` and
  `kalshi_private_key` (the PEM text), then `python -m trading.kalshi_client` runs a read-only
  auth check.
