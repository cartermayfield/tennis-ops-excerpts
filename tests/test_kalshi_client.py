"""trading/kalshi_client.py with no credentials and no network.

A throwaway RSA key is generated per test session and injected into the client; HTTP goes to a
fake session that replays canned responses and records every request.
"""
import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from trading import kalshi_client as kc

KEY_ID = "test-key-id"


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def verify(public_key, sig_b64: str, message: bytes) -> None:
    """Raises InvalidSignature unless sig is RSA-PSS/SHA-256 over `message` (Kalshi's scheme)."""
    public_key.verify(base64.b64decode(sig_b64), message,
                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                  salt_length=padding.PSS.DIGEST_LENGTH),
                      hashes.SHA256())


class FakeResp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else json.dumps(payload))

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


@pytest.fixture
def sleeps(monkeypatch):
    out = []
    monkeypatch.setattr(kc.time, "sleep", out.append)    # record backoff instead of waiting
    return out


@pytest.fixture
def client(rsa_key):
    def make(*responses):
        c = kc.KalshiClient(key_id=KEY_ID, private_key=rsa_key)
        c.s = FakeSession(responses)
        return c
    return make


# ---- key loading ----
def test_load_key_from_toml(tmp_path, rsa_key):
    pem = rsa_key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()).decode()
    f = tmp_path / "secrets.toml"
    f.write_text(f'kalshi_key_id = "{KEY_ID}"\nkalshi_private_key = """{pem}"""\n')
    key_id, key = kc._load_key(f)
    assert key_id == KEY_ID
    assert key.public_key().public_numbers() == rsa_key.public_key().public_numbers()


def test_missing_secrets_file_says_what_it_needs(tmp_path):
    with pytest.raises(FileNotFoundError, match="kalshi_key_id and kalshi_private_key"):
        kc._load_key(tmp_path / "secrets.toml")


# ---- signing ----
def test_signature_verifies_over_timestamp_method_path(client, rsa_key):
    c = client()
    ts, path = "1700000000123", "/trade-api/v2/portfolio/balance"
    sig = c._sign(ts, "get", path)
    verify(rsa_key.public_key(), sig, b"1700000000123GET/trade-api/v2/portfolio/balance")
    # any other message must fail: lowercase method, query string, different timestamp
    for wrong in (b"1700000000123get/trade-api/v2/portfolio/balance",
                  b"1700000000123GET/trade-api/v2/portfolio/balance?limit=1",
                  b"1700000000124GET/trade-api/v2/portfolio/balance"):
        with pytest.raises(InvalidSignature):
            verify(rsa_key.public_key(), sig, wrong)


def test_request_headers_sign_path_without_query(client, rsa_key):
    c = client(FakeResp(200, {"orders": [{"order_id": "o1"}]}))
    assert c.resting_orders(ticker="TEST-A") == [{"order_id": "o1"}]
    call = c.s.calls[0]
    path = "/trade-api/v2/portfolio/orders"
    assert call["url"] == kc.BASE + path
    assert call["params"] == {"status": "resting", "ticker": "TEST-A"}  # query sent separately...
    h = call["headers"]
    assert h["KALSHI-ACCESS-KEY"] == KEY_ID
    assert h["KALSHI-ACCESS-TIMESTAMP"].isdigit() and len(h["KALSHI-ACCESS-TIMESTAMP"]) == 13  # ms
    verify(rsa_key.public_key(), h["KALSHI-ACCESS-SIGNATURE"],             # ...and not signed
           (h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + path).encode())


# ---- retry / backoff / fail-closed ----
def test_retries_429_and_5xx_then_succeeds(client, rsa_key, sleeps):
    c = client(FakeResp(429, text="slow down"), FakeResp(503, text="busy"),
               FakeResp(200, {"balance": 12345}))
    assert c.balance() == {"balance": 12345}
    assert sleeps == [2, 4]                                # capped exponential backoff
    assert len(c.s.calls) == 3
    for call in c.s.calls:                                 # every attempt freshly signed
        h = call["headers"]
        verify(rsa_key.public_key(), h["KALSHI-ACCESS-SIGNATURE"],
               (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/v2/portfolio/balance").encode())


def test_raises_after_retries_exhausted(client, sleeps):
    c = client(*[FakeResp(503, text="unavailable")] * kc.MAX_ATTEMPTS)
    with pytest.raises(kc.KalshiAPIError, match="503 after 4 attempts") as ei:
        c.balance()
    assert ei.value.status == 503
    assert len(c.s.calls) == 4
    assert sleeps == [2, 4, 8]                             # no sleep after the final attempt


def test_non_retryable_error_raises_immediately(client, sleeps):
    c = client(FakeResp(401, text="bad signature"))
    with pytest.raises(kc.KalshiAPIError, match="401: bad signature") as ei:
        c.balance()
    assert ei.value.status == 401 and len(c.s.calls) == 1 and sleeps == []


def test_api_error_is_a_runtime_error():
    assert issubclass(kc.KalshiAPIError, RuntimeError)    # existing `except RuntimeError` still works


def test_outage_never_reads_as_empty_positions(client, sleeps):
    # Before the fix, exhausted retries returned {} and positions() reported "no positions".
    c = client(*[FakeResp(500, text="err")] * kc.MAX_ATTEMPTS)
    with pytest.raises(kc.KalshiAPIError):
        c.positions()


def test_pagination_failure_raises_instead_of_partial_list(client, sleeps):
    c = client(FakeResp(200, {"markets": [{"ticker": "TEST-A"}], "cursor": "c1"}),
               *[FakeResp(502, text="bad gateway")] * kc.MAX_ATTEMPTS)
    with pytest.raises(kc.KalshiAPIError):
        c.markets(status="open")


def test_empty_2xx_body_is_empty_dict(client):
    c = client(FakeResp(204, text=""))
    assert c.cancel_order("ord-1", confirm=True) == {}
    assert c.s.calls[0]["method"] == "DELETE"
    assert c.s.calls[0]["url"].endswith("/portfolio/events/orders/ord-1")


# ---- order guards ----
ORDER = dict(ticker="TEST-A", side="yes", action="buy", count=3, price_cents=42)


def test_unconfirmed_orders_are_refused_before_any_request(client):
    c = client()
    with pytest.raises(PermissionError):
        c.create_order(**ORDER)
    with pytest.raises(PermissionError):
        c.create_order(**ORDER, confirm=False)
    with pytest.raises(PermissionError):
        c.cancel_order("ord-1")
    assert c.s.calls == []                                 # nothing signed, nothing sent


@pytest.mark.parametrize("bad", [dict(action="Buy"), dict(action="sel"), dict(side="no"),
                                 dict(price_cents=0), dict(price_cents=100), dict(count=0)])
def test_malformed_orders_are_refused_before_any_request(client, bad):
    c = client()
    with pytest.raises(ValueError):
        c.create_order(**{**ORDER, **bad}, confirm=True)
    assert c.s.calls == []


def test_confirmed_order_body(client):
    c = client(FakeResp(201, {"order": {"order_id": "ord-9"}}))
    out = c.create_order(**ORDER, client_order_id="cid-1", confirm=True)
    assert out == {"order": {"order_id": "ord-9"}}
    call = c.s.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/portfolio/events/orders")
    assert call["json"] == {"ticker": "TEST-A", "side": "bid", "count": "3.00", "price": "0.4200",
                            "time_in_force": "good_till_canceled",
                            "self_trade_prevention_type": "maker", "post_only": True,
                            "client_order_id": "cid-1"}


def test_retried_order_reuses_client_order_id(client, sleeps):
    c = client(FakeResp(503, text="busy"), FakeResp(201, {"order": {"order_id": "ord-9"}}))
    c.create_order(**{**ORDER, "action": "sell"}, confirm=True)
    first, second = (call["json"] for call in c.s.calls)
    assert first == second and first["side"] == "ask"
    assert first["client_order_id"].startswith("order-")  # generated once, not per attempt


# ---- response normalisation ----
def test_orderbook_handles_dollar_and_cent_shapes(client):
    c = client(FakeResp(200, {"orderbook_fp": {"yes_dollars": [["0.4200", "10"]],
                                               "no_dollars": [["0.5500", "4"]]}}),
               FakeResp(200, {"orderbook": {"yes": [[42, 10]], "no": None}}))
    assert c.orderbook("TEST-A") == {"yes": [["0.4200", "10"]], "no": [["0.5500", "4"]]}
    assert c.orderbook("TEST-A") == {"yes": [[42, 10]], "no": []}


def test_positions_parses_string_sizes_and_drops_flat(client):
    c = client(FakeResp(200, {"market_positions": [
        {"ticker": "A", "position_fp": "3.00"}, {"ticker": "B", "position_fp": "0.00"},
        {"ticker": "C", "position": -2}, {"ticker": "D", "position_fp": "n/a"}]}))
    assert [p["ticker"] for p in c.positions()] == ["A", "C"]


def test_markets_follows_cursor_and_drops_empty_filters(client):
    c = client(FakeResp(200, {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c1"}),
               FakeResp(200, {"markets": [{"ticker": "C"}], "cursor": ""}))
    assert [m["ticker"] for m in c.markets(status="open")] == ["A", "B", "C"]
    assert c.s.calls[0]["params"] == {"status": "open", "limit": 200}
    assert c.s.calls[1]["params"] == {"status": "open", "limit": 200, "cursor": "c1"}
