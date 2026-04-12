"""
kalshi_rest.py — Kalshi REST API wrapper for The Syndicate.

Auth: RSA-PSS message signing.
  Message = "{timestamp_ms}{METHOD}/trade-api/v2{path}"
  Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP

Rate limiter: token bucket enforcing 30 req/sec (Advanced tier).
  Basic tier = 10 read / 5 write. Set RATE_LIMIT_RPS lower if on Basic.

Price convention (Kalshi v2):
  - API orders use yes_price as cents integer (1–99).
  - Portfolio response prices are decimal dollars ("0.6200").
  - Volume responses are in dollar amounts.
"""

import os
import sys
import time
import base64
import json
import threading
import logging

import requests

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

logger = logging.getLogger("syndicate.kalshi_rest")

KALSHI_BASE      = "https://api.elections.kalshi.com/trade-api/v2"
_API_PATH_PREFIX = "/trade-api/v2"

# Advanced tier: 30 req/sec. Lower to 10 if on Basic plan.
RATE_LIMIT_RPS = 30


# ---------------------------------------------------------------------------
# Token bucket rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token bucket: allows up to `rps` calls per second."""

    def __init__(self, rps: int):
        self._rps    = rps
        self._tokens = float(rps)
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def acquire(self):
        with self._lock:
            now     = time.monotonic()
            elapsed = now - self._last
            self._last   = now
            self._tokens = min(self._rps, self._tokens + elapsed * self._rps)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                sleep_for    = (1.0 - self._tokens) / self._rps
                time.sleep(sleep_for)
                self._tokens = 0.0


_limiter = _RateLimiter(RATE_LIMIT_RPS)


# ---------------------------------------------------------------------------
# Config + key loader
# ---------------------------------------------------------------------------

_private_key_cache = None
_key_lock = threading.Lock()


def _load_config():
    import yaml
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_key_material():
    """Return (key_id, key_path) from syndicate_config.yaml → kalshi section."""
    cfg = _load_config()
    key_id      = cfg["kalshi"]["key_id"]
    key_path_cfg = cfg["kalshi"]["api_key_path"]
    key_path = (
        key_path_cfg if os.path.isabs(key_path_cfg)
        else os.path.join(_SYNDICATE_ROOT, key_path_cfg)
    )
    return key_id, key_path


def _load_private_key(key_path: str):
    global _private_key_cache
    with _key_lock:
        if _private_key_cache is not None:
            return _private_key_cache
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(key_path, "rb") as f:
            _private_key_cache = load_pem_private_key(f.read(), password=None)
        return _private_key_cache


def _get_signed_headers(method: str, path: str) -> dict:
    """
    Build RSA-PSS signed headers for Kalshi private endpoints.
    path: endpoint path WITHOUT base URL, e.g. "/portfolio/balance"
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_id, key_path = _get_key_material()
    private_key = _load_private_key(key_path)

    timestamp_ms = str(int(time.time() * 1000))
    full_path    = f"{_API_PATH_PREFIX}{path}"
    message      = f"{timestamp_ms}{method.upper()}{full_path}"

    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type":            "application/json",
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: dict = None) -> dict:
    _limiter.acquire()
    url     = f"{KALSHI_BASE}{endpoint}"
    headers = _get_signed_headers("GET", endpoint)
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 401:
            return {"error": "Auth failed", "status": 401, "detail": resp.text[:300]}
        if resp.status_code == 403:
            return {"error": "Forbidden", "status": 403, "detail": resp.text[:200]}
        if resp.status_code == 429:
            logger.warning("[REST] Rate limited on GET %s", endpoint)
            time.sleep(1)
            return {"error": "Rate limited", "status": 429}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "status": resp.status_code,
                    "detail": resp.text[:300]}
        return resp.json()
    except Exception as e:
        return {"error": str(e), "status": 0}


def _post(endpoint: str, payload: dict) -> dict:
    _limiter.acquire()
    url     = f"{KALSHI_BASE}{endpoint}"
    headers = _get_signed_headers("POST", endpoint)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 401:
            return {"error": "Auth failed", "status": 401, "detail": resp.text[:300]}
        if resp.status_code == 403:
            return {"error": "Forbidden", "status": 403, "detail": resp.text[:200]}
        if resp.status_code == 429:
            logger.warning("[REST] Rate limited on POST %s", endpoint)
            time.sleep(1)
            return {"error": "Rate limited", "status": 429}
        if resp.status_code not in (200, 201):
            return {"error": f"HTTP {resp.status_code}", "status": resp.status_code,
                    "detail": resp.text[:300]}
        return resp.json()
    except Exception as e:
        return {"error": str(e), "status": 0}


def _delete(endpoint: str) -> dict:
    _limiter.acquire()
    url     = f"{KALSHI_BASE}{endpoint}"
    headers = _get_signed_headers("DELETE", endpoint)
    try:
        resp = requests.delete(url, headers=headers, timeout=15)
        if resp.status_code == 401:
            return {"error": "Auth failed", "status": 401, "detail": resp.text[:300]}
        if resp.status_code == 403:
            return {"error": "Forbidden", "status": 403, "detail": resp.text[:200]}
        if resp.status_code == 429:
            time.sleep(1)
            return {"error": "Rate limited", "status": 429}
        if resp.status_code not in (200, 201, 204):
            return {"error": f"HTTP {resp.status_code}", "status": resp.status_code,
                    "detail": resp.text[:300]}
        if resp.status_code == 204 or not resp.content:
            return {"status": "cancelled"}
        return resp.json()
    except Exception as e:
        return {"error": str(e), "status": 0}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Series discovery — never hardcode guesses
# ---------------------------------------------------------------------------

# All series the Syndicate trades — probed on every startup.
# Any series returning ≥1 open market is included automatically.
# KXMVESPORTSMULTIGAMEEXTENDED / KXMVECROSSCATEGORY deliberately omitted:
# they contain 20,000+ illiquid micro-prop markets that flood pagination.
_KNOWN_SPORTS_SERIES = [
    # Tennis — Atlas owns match winners; Syndicate watches for cross-signals
    "KXATPMATCH",    # ATP Tennis Match Winner
    "KXWTAMATCH",    # WTA Tennis Match Winner
    # Golf
    "KXPGATOUR",     # PGA Tour tournament winner
    "KXPGAR1LEAD",   # PGA Round 1 leader
    "KXPGAR2LEAD",   # PGA Round 2 leader
    "KXPGAR3LEAD",   # PGA Round 3 leader
    "KXPGAR4LEAD",   # PGA Round 4 leader
    # Team sports
    "KXNBA",         # NBA
    "KXMLB",         # MLB
    "KXNHL",         # NHL
    "KXNFL",         # NFL
    "KXSOCCER",      # Soccer / MLS
    "KXNCAA",        # College sports
    # NOTE: Crypto, politics, and economics series removed for 30-day validation phase.
    # Re-enable after 50 sports trades with positive P&L:
    # KXBTCD, KXETHUSD, KXPOL, KXECON, KXFED, KXCPI, KXELEC, KXAPPROVAL
]

_active_series_cache: list[str] = []
_active_series_ts:    float     = 0.0
_SERIES_CACHE_TTL:    float     = 300.0   # re-probe every 5 min


def discover_active_series() -> list[str]:
    """
    Probe all known series prefixes and return those with ≥1 open market.
    Result is cached for 5 minutes. Logs discovered series at INFO level.
    """
    global _active_series_cache, _active_series_ts
    now = time.time()
    if _active_series_cache and now - _active_series_ts < _SERIES_CACHE_TTL:
        return _active_series_cache

    active = []
    for series in _KNOWN_SPORTS_SERIES:
        try:
            data = _get("/markets", params={"status": "open", "series_ticker": series, "limit": 1})
            if data.get("markets"):
                active.append(series)
        except Exception:
            pass

    _active_series_cache = active
    _active_series_ts    = now
    logger.info("[REST] discover_active_series: %d active → %s", len(active), active)
    return active


def get_sports_markets(limit: int = 100) -> list:
    """
    Fetch all open sports markets from Kalshi by querying known sports series.

    Match winner markets only — set/game/doubles series excluded (low volume).

    Price fields (confirmed from live API):
      yes_bid_dollars / yes_ask_dollars — decimal dollar strings e.g. "0.3500"
      last_price_dollars                — last traded price
      volume_fp                         — contracts traded (not dollars)
      Dollar volume = volume_fp × mid_price (approximated here)

    Returns list of dicts: ticker, yes_price, volume_dollars, title, expiry, series_ticker.
    """
    SPORTS_SERIES = discover_active_series()

    seen        = set()
    all_markets = []

    for series in SPORTS_SERIES:
        cursor = None
        _page = 0
        while _page < 5:  # safety cap: max 500 markets per series
            _page += 1
            params = {
                "status":        "open",
                "series_ticker": series,
                "limit":         100,
            }
            if cursor:
                params["cursor"] = cursor

            data = _get("/markets", params=params)
            if "error" in data:
                logger.warning("[REST] series %s error: %s", series, data.get("error"))
                break

            raw = data.get("markets", [])
            for m in raw:
                ticker = m.get("ticker", "")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)

                try:
                    yes_bid = float(m.get("yes_bid_dollars") or 0)
                    yes_ask = float(m.get("yes_ask_dollars") or 0)
                    if yes_bid > 0 and yes_ask > 0:
                        yes_price = (yes_bid + yes_ask) / 2.0
                    else:
                        yes_price = float(m.get("last_price_dollars") or 0)
                except (ValueError, TypeError):
                    yes_price = 0.0

                try:
                    volume_fp      = float(m.get("volume_fp") or m.get("volume_24h_fp") or 0)
                    volume_dollars = volume_fp * yes_price if yes_price > 0 else 0.0
                except (ValueError, TypeError):
                    volume_dollars = 0.0

                all_markets.append({
                    "ticker":         ticker,
                    "title":          m.get("title", ""),
                    "yes_price":      yes_price,
                    "volume_dollars": volume_dollars,
                    "expiry":         m.get("close_time") or m.get("expiration_time", ""),
                    "event_ticker":   m.get("event_ticker", ""),
                    "series_ticker":  series,
                })

            cursor = data.get("cursor")
            if not cursor or len(raw) < 100:
                break

    logger.info("[REST] get_sports_markets: %d markets across %d series.",
                len(all_markets), len(SPORTS_SERIES))
    return all_markets


# ---------------------------------------------------------------------------
# Series exclusions — Atlas owns tennis; Syndicate must not overlap
# ---------------------------------------------------------------------------

EXCLUDED_SERIES = {
    "KXATPMATCH",   # ATP Tennis Match Winner — owned by Atlas
    "KXWTAMATCH",   # WTA Tennis Match Winner — owned by Atlas
}


def get_all_markets(max_pages: int = 100) -> list:
    """
    Fetch all open Kalshi markets across every series in _KNOWN_SPORTS_SERIES.

    Uses targeted series_ticker queries instead of blind global pagination to
    avoid KXMVESPORTSMULTIGAMEEXTENDED / KXMVECROSSCATEGORY flooding all pages
    before tradeable markets (KXPGATOUR, KXNBA, KXBTCD, etc.) are reached.

    Tennis series (KXATPMATCH, KXWTAMATCH) results are still returned here for
    shared_state population; scan_engine excludes them from the agent sweep.

    Returns list of dicts: ticker, yes_price, volume_dollars, title, expiry, series_ticker.
    max_pages param kept for API compatibility (unused — series loop has its own cap).
    """
    markets = get_sports_markets()
    logger.info("[REST] get_all_markets: %d total open markets fetched.", len(markets))
    return markets


def _place_order(ticker: str, side: str, quantity: int,
                 price_cents: int, order_type: str, action: str) -> dict:
    """Internal order placer. price_cents: integer 2–98."""
    if side not in ("yes", "no"):
        logger.error("[REST] Invalid side '%s'", side)
        return {"error": f"Invalid side: {side}"}
    if not (2 <= price_cents <= 98):
        logger.error("[REST] Invalid price_cents %d", price_cents)
        return {"error": f"price_cents out of range: {price_cents}"}
    if not (1 <= quantity <= 100):
        logger.error("[REST] Invalid quantity %d", quantity)
        return {"error": f"quantity out of range: {quantity}"}

    # Kalshi always uses yes_price (cents). NO orders: yes_price = 100 - no_price.
    yes_price = price_cents if side == "yes" else (100 - price_cents)

    payload = {
        "ticker":    ticker.strip(),
        "action":    action,   # "buy" or "sell"
        "side":      side,
        "count":     quantity,
        "type":      order_type,
        "yes_price": yes_price,
    }

    logger.info("[REST] %s %s %dx %s @ %d¢ (yes_price=%d¢)",
                action.upper(), side.upper(), quantity, ticker, price_cents, yes_price)

    result = _post("/portfolio/orders", payload)
    if "error" in result:
        logger.error("[REST] Order failed: %s", result)
        return result

    order    = result.get("order", result)
    order_id = order.get("order_id", "unknown")
    status   = order.get("status", "unknown")
    logger.info("[REST] Order placed — id=%s status=%s", order_id, status)
    return result


def _dollars_to_cents(price_dollars: float) -> int:
    """Convert decimal dollar price (0.62) to cents integer (62). Clamps 2–98."""
    cents = round(price_dollars * 100)
    return max(2, min(98, int(cents)))


def place_limit_buy(ticker: str, side: str, quantity: int,
                    price_dollars: float) -> dict:
    """Place a limit BUY order. price_dollars: e.g. 0.45 for 45 cents."""
    return _place_order(ticker, side, quantity,
                        _dollars_to_cents(price_dollars), "limit", "buy")


def place_limit_sell(ticker: str, side: str, quantity: int,
                     price_dollars: float) -> dict:
    """Place a limit SELL order to close a position."""
    return _place_order(ticker, side, quantity,
                        _dollars_to_cents(price_dollars), "limit", "sell")


def place_market_buy(ticker: str, side: str, quantity: int,
                     yes_price: float) -> dict:
    """
    Place an aggressive limit BUY at 5¢ above current price.
    Kalshi has no true market orders; aggressive limit is the approved pattern.
    yes_price: current price of the side being bought (decimal dollars 0.0–1.0).
    """
    aggressive_price = min(0.99, yes_price + 0.05)
    logger.info("[REST] AGG LIMIT BUY %s %dx %s @ %.2f (base=%.2f)",
                side.upper(), quantity, ticker, aggressive_price, yes_price)
    return _place_order(ticker, side, quantity,
                        _dollars_to_cents(aggressive_price), "limit", "buy")


def cancel_order(order_id: str) -> bool:
    """Cancel a resting order. Returns True on success."""
    if not order_id or not str(order_id).strip():
        return False
    result  = _delete(f"/portfolio/orders/{order_id.strip()}")
    success = "error" not in result
    if not success:
        logger.error("[REST] Cancel failed for %s: %s", order_id, result)
    return success


def get_positions() -> list:
    """
    GET /portfolio/positions
    Returns list of active (non-zero) positions.
    """
    data = _get("/portfolio/positions")
    if "error" in data:
        logger.error("[REST] get_positions error: %s", data)
        return []

    raw    = data.get("market_positions", [])
    result = []
    for pos in raw:
        try:
            pos_fp = float(pos.get("position_fp", 0) or 0)
        except (ValueError, TypeError):
            pos_fp = 0.0
        if pos_fp == 0.0:
            continue

        qty  = abs(int(pos_fp))
        side = "yes" if pos_fp > 0 else "no"

        try:
            market_value = round(float(pos.get("market_exposure_dollars", 0) or 0), 2)
        except (ValueError, TypeError):
            market_value = 0.0

        try:
            total_cost = float(pos.get("total_traded_dollars", 0) or 0)
            avg_entry  = round(total_cost / qty, 4) if qty > 0 else 0.0
        except (ValueError, TypeError):
            avg_entry = 0.0

        result.append({
            "ticker":       pos.get("ticker", ""),
            "side":         side,
            "quantity":     qty,
            "avg_entry":    avg_entry,    # decimal dollars
            "market_value": market_value,
        })
    return result


def get_balance() -> float:
    """
    GET /portfolio/balance
    Returns available balance in dollars.
    """
    data = _get("/portfolio/balance")
    if "error" in data:
        logger.error("[REST] get_balance error: %s", data)
        return 0.0
    try:
        # Kalshi v2: balance is in cents (integer)
        return round(int(data.get("balance", 0)) / 100.0, 2)
    except (ValueError, TypeError):
        return 0.0


def get_order_status(order_id: str) -> dict:
    """
    GET /portfolio/orders/{order_id}
    Returns order status dict, or error dict.
    """
    data = _get(f"/portfolio/orders/{order_id.strip()}")
    if "error" in data:
        return data
    return data.get("order", data)
