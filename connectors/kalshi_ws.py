"""
kalshi_ws.py — Kalshi WebSocket connector for The Syndicate.

Connects to wss://api.elections.kalshi.com/trade-api/ws/v2
Subscribes to 'ticker' channel for all open sports markets.
Writes price updates to shared_state and fires on_tick_callback for hot path.
Runs as a daemon thread — auto-reconnects on any failure.

Auth: RSA-PSS message signing.
  Headers sent on WS handshake: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE,
  KALSHI-ACCESS-TIMESTAMP.
  Signing message = "{timestamp_ms}GET/trade-api/ws/v2"

Callback injection (by main.py):
  ws._on_tick_callback = _on_tick   # called on every price tick
"""

import os
import sys
import json
import time
import base64
import threading
import logging

import websocket  # websocket-client library

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from core.shared_state import state, MarketData

logger = logging.getLogger("syndicate.kalshi_ws")

WS_URL           = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_API_PATH_PREFIX = "/trade-api/ws/v2"


# ---------------------------------------------------------------------------
# RSA-PSS auth headers
# ---------------------------------------------------------------------------

_private_key_cache = None
_key_lock = threading.Lock()


def _load_config():
    """Load syndicate_config.yaml."""
    import yaml
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_private_key(key_path: str):
    global _private_key_cache
    with _key_lock:
        if _private_key_cache is not None:
            return _private_key_cache
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(key_path, "rb") as f:
            _private_key_cache = load_pem_private_key(f.read(), password=None)
        return _private_key_cache


def _get_signed_headers(key_id: str, key_path: str) -> dict:
    """Build Kalshi RSA-PSS signed headers for WS handshake."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key  = _load_private_key(key_path)
    timestamp_ms = str(int(time.time() * 1000))
    message      = f"{timestamp_ms}GET{_API_PATH_PREFIX}"

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
    }


# ---------------------------------------------------------------------------
# Subscription helpers
# ---------------------------------------------------------------------------

def _build_subscribe_msg(msg_id: int, tickers: list) -> str:
    """Build Kalshi WS subscribe command."""
    return json.dumps({
        "id":     msg_id,
        "cmd":    "subscribe",
        "params": {
            "channels":       ["ticker"],
            "market_tickers": tickers,
        },
    })


def _parse_ticker_msg(msg: dict) -> tuple:
    """
    Parse a Kalshi 'ticker' WebSocket message.
    Returns (ticker, yes_price_float, no_bid_float, volume_dollars_float) or None.

    Kalshi ticker fields (WS v2):
      yes_bid, yes_ask, no_bid, no_ask — cents integers (1-99)
      yes_price_dollars — string e.g. "0.6200" (mid-market, decimal dollars)
      volume — total contracts traded
    """
    msg_data = msg.get("msg", {})
    if not msg_data:
        return None

    ticker = msg_data.get("market_ticker") or msg_data.get("ticker", "")
    if not ticker:
        return None

    # yes_price_dollars is the clean mid-market price
    yes_price_raw = msg_data.get("yes_price_dollars")
    if yes_price_raw is not None:
        try:
            yes_price = float(yes_price_raw)
        except (ValueError, TypeError):
            yes_price = 0.0
    else:
        # Fallback 1: last_price (cents int)
        last_price_raw = msg_data.get("last_price") or msg_data.get("last_yes_price")
        if last_price_raw is not None:
            try:
                yes_price = float(last_price_raw) / 100.0
            except (ValueError, TypeError):
                yes_price = 0.0
        else:
            # Fallback 2: bid/ask mid
            yes_bid = float(msg_data.get("yes_bid", 0) or 0)
            yes_ask = float(msg_data.get("yes_ask", 0) or 0)
            if yes_bid > 0 and yes_ask > 0:
                yes_price = ((yes_bid + yes_ask) / 2) / 100.0
            else:
                yes_price = 0.0

    no_bid_raw = msg_data.get("no_bid", 0)
    try:
        no_bid = float(no_bid_raw or 0) / 100.0
    except (ValueError, TypeError):
        no_bid = 0.0

    # Volume: prefer volume_dollars if present, else estimate from contracts
    vol_raw = msg_data.get("dollar_volume") or msg_data.get("volume_dollars")
    if vol_raw is not None:
        try:
            volume_dollars = float(vol_raw)
        except (ValueError, TypeError):
            volume_dollars = 0.0
    else:
        contracts      = float(msg_data.get("volume", 0) or 0)
        volume_dollars = contracts * yes_price if yes_price > 0 else 0.0

    if yes_price <= 0:
        return None

    return ticker, yes_price, no_bid, volume_dollars


# ---------------------------------------------------------------------------
# KalshiWS — main connector class
# ---------------------------------------------------------------------------

class KalshiWS:
    """
    WebSocket connector that subscribes to Kalshi ticker feed for sports markets.
    Call .start() to launch as a daemon thread.

    main.py injects the tick callback after construction:
        ws._on_tick_callback = _on_tick
    """

    def __init__(self, tickers: list, velocity_window: float = 60.0):
        cfg = _load_config()
        self.key_id = cfg["kalshi"]["key_id"]
        key_path_cfg = cfg["kalshi"]["api_key_path"]
        self.key_path = (
            key_path_cfg if os.path.isabs(key_path_cfg)
            else os.path.join(_SYNDICATE_ROOT, key_path_cfg)
        )
        self.ws_url          = cfg["kalshi"].get("ws_url", WS_URL)
        self.tickers         = tickers
        self.velocity_window = velocity_window

        # Injected by main.py after construction
        self._on_tick_callback = None

        self._ws       = None
        self._last_seq = -1
        self._msg_id   = 1
        self._running  = False
        self._thread   = None

    # -------------------------------------------------------------------------
    # WebSocket callbacks
    # -------------------------------------------------------------------------

    def _on_open(self, ws):
        logger.info("[KalshiWS] Connected. Subscribing to %d tickers.", len(self.tickers))
        self._last_seq = -1
        self._subscribe(ws)

    def _subscribe(self, ws):
        if not self.tickers:
            logger.warning("[KalshiWS] No tickers to subscribe to.")
            return
        # Kalshi WS supports up to 200 tickers per subscribe message
        batch_size = 200
        for i in range(0, len(self.tickers), batch_size):
            batch = self.tickers[i:i + batch_size]
            ws.send(_build_subscribe_msg(self._msg_id, batch))
            self._msg_id += 1

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")

        # ── Sequence gap detection ──
        seq = msg.get("seq")
        if seq is not None:
            if self._last_seq >= 0 and seq != self._last_seq + 1:
                logger.warning(
                    "[KalshiWS] Sequence gap: expected %d got %d — resubscribing.",
                    self._last_seq + 1, seq,
                )
                self._subscribe(ws)
            self._last_seq = seq

        # ── Ticker update ──
        if msg_type == "ticker":
            parsed = _parse_ticker_msg(msg)
            if parsed:
                ticker, yes_price, no_bid, volume_dollars = parsed
                ts = time.time()
                state.upsert_market(ticker, yes_price, no_bid, volume_dollars, ts)

                # Dispatch to registered consumers (scalper + scan engine)
                if self._on_tick_callback is not None:
                    try:
                        self._on_tick_callback(ticker, yes_price, volume_dollars)
                    except Exception as e:
                        logger.error("[KalshiWS] tick callback error for %s: %s", ticker, e)

        # ── Subscription confirmation ──
        elif msg_type in ("subscribed", "subscription_confirmed"):
            logger.info("[KalshiWS] Subscription confirmed: %s",
                        msg.get("msg", {}).get("channel", ""))

        # ── Pong (server-initiated ping → we respond) ──
        elif msg_type == "ping":
            try:
                ws.send(json.dumps({"type": "pong"}))
            except Exception:
                pass

        # ── Error ──
        elif msg_type == "error":
            logger.error("[KalshiWS] Server error: %s", msg)

    def _on_error(self, ws, error):
        logger.error("[KalshiWS] Error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("[KalshiWS] Closed: %s %s", close_status_code, close_msg)

    def _on_ping(self, ws, data):
        """Handle WebSocket protocol-level pings — reply with pong frame."""
        try:
            ws.send(data, websocket.ABNF.OPCODE_PONG)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Connection loop with exponential backoff
    # -------------------------------------------------------------------------

    def _run_loop(self):
        backoff = 1
        while self._running:
            try:
                headers  = _get_signed_headers(self.key_id, self.key_path)
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_ping=self._on_ping,
                )
                logger.info("[KalshiWS] Connecting to %s", self.ws_url)
                self._ws.run_forever(
                    ping_interval=0,   # server pings us every 10s
                    ping_timeout=None,
                    reconnect=0,       # we handle reconnect ourselves
                )
            except Exception as e:
                logger.error("[KalshiWS] Connection exception: %s", e)

            if not self._running:
                break

            logger.info("[KalshiWS] Reconnecting in %ds...", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

        logger.info("[KalshiWS] Run loop exited.")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def update_tickers(self, new_tickers: list):
        """Hot-reload ticker list. Resubscribes immediately if connected."""
        self.tickers = new_tickers
        if self._ws and self._ws.sock and self._ws.sock.connected:
            self._subscribe(self._ws)

    def start(self) -> threading.Thread:
        """Launch connector as a daemon thread. Returns the thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop,
            name="kalshi-ws",
            daemon=True,
        )
        self._thread.start()
        logger.info("[KalshiWS] Daemon thread started.")
        return self._thread

    def stop(self):
        """Signal the run loop to stop and close the WebSocket."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
