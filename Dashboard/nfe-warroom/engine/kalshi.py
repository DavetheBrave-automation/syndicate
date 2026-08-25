"""Kalshi trade API client.

Uses RSA-PSS request signing per Kalshi's current auth scheme:
  https://trading-api.readme.io/reference/createorder

Required env:
  KALSHI_API_KEY_ID         — UUID of your API key
  KALSHI_PRIVATE_KEY_PATH   — path to PEM-encoded RSA private key
  KALSHI_API_BASE           — https://api.elections.kalshi.com/trade-api/v2

If keys absent, returns mock data so paper mode runs end-to-end without creds.
"""
import time
import base64
from pathlib import Path
import requests

from app.config import (
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_API_BASE
)


_private_key = None


def _load_private_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    if not KALSHI_PRIVATE_KEY_PATH:
        return None
    p = Path(KALSHI_PRIVATE_KEY_PATH)
    if not p.exists():
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        with open(p, "rb") as f:
            _private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        return _private_key
    except Exception as e:
        print(f"[kalshi] failed to load private key: {e}")
        return None


def _sign(method: str, path: str, ts_ms: str) -> str | None:
    pk = _load_private_key()
    if pk is None:
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    msg = f"{ts_ms}{method.upper()}{path}".encode()
    sig = pk.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _headers(method: str, path: str) -> dict:
    if not KALSHI_API_KEY_ID:
        return {}
    ts_ms = str(int(time.time() * 1000))
    sig = _sign(method, path, ts_ms)
    if sig is None:
        return {}
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }


def has_creds() -> bool:
    return bool(KALSHI_API_KEY_ID) and _load_private_key() is not None


def _request(method: str, path: str, params: dict = None, json_body: dict = None):
    """Returns parsed JSON or None on failure."""
    if not has_creds():
        return None
    url = KALSHI_API_BASE + path
    try:
        r = requests.request(
            method, url,
            headers=_headers(method, "/trade-api/v2" + path),
            params=params, json=json_body, timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[kalshi] {method} {path} failed: {e}")
        return None


# ---------- public methods ----------

def get_balance() -> dict | None:
    """Returns {balance: cents, payout: cents}."""
    return _request("GET", "/portfolio/balance")


def get_positions() -> list:
    """Returns list of open position dicts."""
    res = _request("GET", "/portfolio/positions")
    if not res:
        return []
    return res.get("market_positions", [])


def get_orders(status: str = "resting") -> list:
    res = _request("GET", "/portfolio/orders", params={"status": status})
    if not res:
        return []
    return res.get("orders", [])


def list_markets(status: str = "open", limit: int = 200, cursor: str = None) -> dict | None:
    """Paginated list of markets. Use cursor for continuation."""
    params = {"status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _request("GET", "/markets", params=params)


def get_market(ticker: str) -> dict | None:
    res = _request("GET", f"/markets/{ticker}")
    return res.get("market") if res else None


def place_order(
    ticker: str, side: str, action: str, count: int,
    yes_price: int = None, no_price: int = None,
    order_type: str = "limit",
) -> dict | None:
    """Place an order.

    side: 'yes' | 'no'
    action: 'buy' | 'sell'
    count: contracts
    yes_price / no_price: limit price in cents (1-99)
    order_type: 'limit' | 'market'
    """
    body = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "type": order_type,
        "client_order_id": f"warroom-{int(time.time()*1000)}",
    }
    if order_type == "limit":
        if side == "yes":
            body["yes_price"] = yes_price
        else:
            body["no_price"] = no_price
    return _request("POST", "/portfolio/orders", json_body=body)


def cancel_order(order_id: str) -> dict | None:
    return _request("DELETE", f"/portfolio/orders/{order_id}")


# ---------- mock data for paper mode without creds ----------

def mock_markets() -> list:
    """Stand-in market data so paper mode runs without API access.

    Format: (ticker, class, yes_bid, yes_ask, last, volume_24h, hours_to_expiry)
    hours_to_expiry=None → random choice.

    Includes realistic-volume Syndicate test markets (prefixed KXWTIW/KXFFR/KXBTC)
    designed to hit GLITCH conviction tier for paper-mode validation.
    """
    import random, time
    base_t = int(time.time())

    samples = [
        # ticker                             class     yb  ya  last  vol     hrs
        # ── Normal market spread (low scores — filtered out as expected) ──
        ("KXWTI-26APR15-T95.00",            "oil",    22, 24, 18,   800,    None),
        ("KXWTI-26APR15-T100.00",           "oil",    14, 16, 12,   1200,   None),
        ("KXGOLD-26APR-T2400",              "gold",   55, 58, 50,   600,    None),
        ("KXNG-26MAY-T3.50",                "natgas", 35, 38, 30,   400,    None),
        ("KXBTC-26APR15-T70K",              "btc",    42, 45, 38,   1500,   None),
        ("KXETH-26APR15-T3500",             "eth",    28, 31, 25,   700,    None),
        ("KXMLB-26-NYY",                    "sports", 12, 14, 11,   5000,   None),
        ("KXNHL-26-VGK",                    "sports",  7,  9,  6,   2200,   None),
        ("KXNBA-26-LAL",                    "sports",  4,  6,  3,   3100,   None),
        ("KXFFR-26MAY-CUT25",               "rates",  18, 21, 16,   900,    None),
        ("KXSPX-26APR-T5500",               "stocks", 65, 68, 62,   1100,   None),
        ("KXUSDJPY-26APR-T160",             "forex",  48, 51, 45,   850,    None),
        ("KXMVESPORTS-S2026XYZ",            "sports", 15, 18, 13,   50,     None),  # blocked
        # ── Syndicate smoke-test markets (realistic vol, price near 50¢) ──
        # These should hit GLITCH tier with appropriate macro signals
        ("KXWTIW-26APR15-T95",              "oil",    55, 59, 52,   50000,  36),
        ("KXFFR-26APR-T5PCT",               "rates",  55, 59, 52,   20000,  60),
        ("KXBTC-26APR-T50K",                "btc",    55, 59, 52,   100000, 36),
    ]
    out = []
    rand_hours = [4, 18, 36, 60, 96, 168, 400]
    for ticker, _, yb, ya, last, vol, hrs in samples:
        out.append({
            "ticker": ticker,
            "yes_bid": yb,
            "yes_ask": ya,
            "no_bid": 100 - ya,
            "no_ask": 100 - yb,
            "last_price": last,
            "volume_24h": vol,
            "hours_to_expiry": hrs if hrs is not None else random.choice(rand_hours),
            "close_time": base_t + 3600 * (hrs or random.choice([4, 18, 36, 60, 96])),
        })
    return out
