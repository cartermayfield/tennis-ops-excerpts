"""Authenticated Kalshi REST client (RSA-PSS request signing).

Read methods power the dashboards and collectors. Write methods (create_order / cancel_order)
require an explicit confirm=True flag so an order can never be placed by accident; the trading
bots pass it only after a human has armed the session.

Credentials come from secrets.toml at the repo root (git-ignored): kalshi_key_id + kalshi_private_key.
Signing scheme (Kalshi): sign  timestamp_ms + METHOD + path  with RSA-PSS/SHA256, send as headers
KALSHI-ACCESS-KEY / KALSHI-ACCESS-SIGNATURE / KALSHI-ACCESS-TIMESTAMP.
"""
from __future__ import annotations

import base64
import time
import tomllib
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

REPO_ROOT = Path(__file__).resolve().parents[1]

BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"
SECRETS = REPO_ROOT / "secrets.toml"
RETRY_STATUS = (429, 500, 502, 503, 504)   # rate-limited / transient server errors
MAX_ATTEMPTS = 4


class KalshiAPIError(RuntimeError):
    """Non-2xx response, or retries exhausted. `status` is the last HTTP status code."""
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _load_key(path=SECRETS):
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} not found: it needs kalshi_key_id and kalshi_private_key")
    with open(path, "rb") as fh:
        creds = tomllib.load(fh)
    key = serialization.load_pem_private_key(
        creds["kalshi_private_key"].encode(), password=None)
    return creds["kalshi_key_id"], key


class KalshiClient:
    def __init__(self, key_id: str | None = None, private_key=None):
        """Credentials default to secrets.toml. Pass key_id + a loaded RSA private_key to inject
        them instead (tests, or a key held in a secrets manager)."""
        if private_key is None:
            key_id, private_key = _load_key()
        self.key_id, self._key = key_id, private_key
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "trading-client/0.1"

    # --- signing ---------------------------------------------------------- #
    def _sign(self, ts: str, method: str, path: str) -> str:
        msg = (ts + method.upper() + path).encode()
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _req(self, method: str, path: str, *, params=None, body=None):
        """Signed request. 429/5xx are retried with capped exponential backoff (2, 4, 8 s), each
        attempt freshly signed. Any other error, or exhausted retries, raises KalshiAPIError:
        an outage must never look like an empty book or zero positions to the caller."""
        url = BASE + path                                  # sign the PATH only (no query string)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            r = self.s.request(method, url, params=params, json=body,
                               headers=self._headers(method, path), timeout=30)
            if 200 <= r.status_code < 300:                 # 201 Created (orders), 200, 204 all OK
                return r.json() if r.text.strip() else {}
            if r.status_code not in RETRY_STATUS:
                raise KalshiAPIError(f"{method} {path} -> {r.status_code}: {r.text[:200]}",
                                     r.status_code)
            if attempt < MAX_ATTEMPTS:                     # no pointless sleep after the last try
                time.sleep(min(8, 2 ** attempt))
        raise KalshiAPIError(f"{method} {path} -> {r.status_code} after {MAX_ATTEMPTS} attempts: "
                             f"{r.text[:200]}", r.status_code)

    # --- reads (dashboard) ------------------------------------------------ #
    def exchange_status(self) -> dict:
        return self._req("GET", f"{API}/exchange/status")

    def markets(self, series_ticker=None, status=None, limit=200, cursor=None) -> list[dict]:
        """All markets matching the filters (auto-paginates)."""
        out = []
        while True:
            p = {k: v for k, v in dict(series_ticker=series_ticker, status=status,
                                       limit=limit, cursor=cursor).items() if v}
            d = self._req("GET", f"{API}/markets", params=p)
            out += d.get("markets", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                return out

    def market(self, ticker: str) -> dict:
        return self._req("GET", f"{API}/markets/{ticker}").get("market", {})

    def orderbook(self, ticker: str, depth=10) -> dict:
        """Normalised resting book: {'yes': [[price$, size], ...], 'no': [...]}. Kalshi nests it under
        `orderbook_fp` with `yes_dollars`/`no_dollars` (dollar prices); older shape was `orderbook`
        with `yes`/`no` (cents). Handle both."""
        d = self._req("GET", f"{API}/markets/{ticker}/orderbook", params={"depth": depth})
        ob = (d.get("orderbook_fp") or d.get("orderbook") or {}) if isinstance(d, dict) else {}
        return {"yes": ob.get("yes_dollars") or ob.get("yes") or [],
                "no": ob.get("no_dollars") or ob.get("no") or []}

    def balance(self) -> dict:
        return self._req("GET", f"{API}/portfolio/balance")

    def resting_orders(self, ticker=None) -> list[dict]:
        p = {"status": "resting"}
        if ticker:
            p["ticker"] = ticker
        return self._req("GET", f"{API}/portfolio/orders", params=p).get("orders", [])

    def positions(self) -> list[dict]:
        """Held market positions (non-zero). Position size is `position_fp` (string)."""
        d = self._req("GET", f"{API}/portfolio/positions")
        def sz(p):
            try:
                return float(p.get("position_fp") or p.get("position") or 0)
            except (TypeError, ValueError):
                return 0.0
        return [p for p in d.get("market_positions", []) if sz(p) != 0]

    def fills(self, min_ts=None, limit=200) -> list[dict]:
        """Authenticated fill records (ground truth for executions; fields incl. fill_id,
        order_id, ticker, action, count_fp, yes_price_dollars, ts). Read-only."""
        p = {"limit": limit}
        if min_ts:
            p["min_ts"] = int(min_ts)
        return self._req("GET", f"{API}/portfolio/fills", params=p).get("fills", [])

    # --- writes (V2 endpoints; require confirm=True) ------------------------------- #
    def create_order(self, *, ticker: str, side: str, action: str, count: int,
                     price_cents: int, order_type="limit", post_only: bool = True,
                     client_order_id=None, confirm: bool = False) -> dict:
        """Place an order via the V2 endpoint POST /portfolio/events/orders.
        Interface unchanged for callers (side 'yes'|'no', action 'buy'|'sell', price_cents
        1..99); mapped here to the V2 body (side 'bid'/'ask', dollar price string).
        post_only=True keeps a limit order a MAKER (rejected rather than crossing). Requires
        confirm=True so it can never fire by accident."""
        if not confirm:
            raise PermissionError("create_order requires confirm=True (safety guard)")
        if side != "yes":                                    # V2 mapping below is YES-side only
            raise ValueError("V2 create_order supports side='yes' only (buy YES = bid)")
        if action not in ("buy", "sell"):                    # a typo must not become a sell
            raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
        if not 1 <= price_cents <= 99 or int(count) < 1:
            raise ValueError(f"bad order: price_cents={price_cents} (1..99), count={count} (>=1)")
        v2_side = "bid" if action == "buy" else "ask"        # bid = buy YES, ask = sell YES
        tif = "good_till_canceled" if order_type == "limit" else "immediate_or_cancel"
        body = {"ticker": ticker, "side": v2_side, "count": f"{int(count)}.00",
                "price": f"{price_cents / 100:.4f}", "time_in_force": tif,
                "self_trade_prevention_type": "maker", "post_only": bool(post_only),
                "client_order_id": client_order_id or f"order-{int(time.time()*1000)}"}
        return self._req("POST", f"{API}/portfolio/events/orders", body=body)

    def cancel_order(self, order_id: str, *, confirm: bool = False) -> dict:
        if not confirm:
            raise PermissionError("cancel_order requires confirm=True (safety guard)")
        return self._req("DELETE", f"{API}/portfolio/events/orders/{order_id}")


if __name__ == "__main__":          # quick auth smoke test (READ ONLY)
    c = KalshiClient()
    print("exchange status:", c.exchange_status())
    try:
        print("balance:", c.balance())
    except Exception as e:
        print("balance read failed (ok if key lacks portfolio scope):", e)
