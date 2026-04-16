"""
diamond.py — DIAMOND, Syndicate crypto specialist agent.

Strategy: Log-normal mispricing on Kalshi cryptocurrency price-level markets.

Kalshi lists binary markets of the form "Will BTC end above $85,000?"
(e.g. ticker KXBTCD-23APR26-85000). DIAMOND fetches the current BTC or ETH
spot price from the CoinGecko public API (no auth required), then computes
the fair-value probability that the asset will close above the strike price
using a log-normal price model with zero drift.

If the computed true probability diverges from Kalshi's implied price by more
than MIN_EDGE (10%), DIAMOND builds and submits a HIGH_CONVICTION signal.
Edges above PROPHECY_EDGE (20%) earn a PROPHECY signal. Both YES and NO sides
are considered — whichever direction is mispriced.

Supported series: KXBTCD (BTC daily), KXBTCW (BTC weekly),
                  KXETHD (ETH daily), KXETHW (ETH weekly).
"""

import math
import time
import logging
import re
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.diamond")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Kalshi crypto series tickers DIAMOND watches
_CRYPTO_SERIES = {"KXBTCD", "KXBTCW", "KXETHD", "KXETHUSD", "KXETHW"}

# Daily volatility assumptions per asset
_VOL_DAILY = {"BTC": 0.04, "ETH": 0.05}

# CoinGecko API IDs
_COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum"}

# Spot price cache: {asset: (price, fetch_timestamp)}
_spot_cache: dict = {}
_CACHE_TTL = 60.0  # seconds — re-fetch at most once per minute

MIN_EDGE = 10.0      # % edge required for HIGH_CONVICTION
PROPHECY_EDGE = 20.0  # % edge for PROPHECY

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_ticker(ticker: str):
    """
    Extract (asset, strike) from Kalshi crypto ticker.
    e.g. "KXBTCD-23APR26-85000" -> ("BTC", 85000.0)
    Returns (None, None) if not parseable.
    """
    m = re.match(r"KX(BTC|ETH)[DW]-\w+-T?(\d+(?:\.\d+)?)", ticker)
    if not m:
        return None, None
    return m.group(1), float(m.group(2))


def _fetch_spot(asset: str) -> float:
    """Fetch current USD price from CoinGecko public API (no auth)."""
    import urllib.request, json  # lazy import — avoid module-level network dep
    coin_id = _COINGECKO_IDS[asset]
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies=usd"
    )
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    return float(data[coin_id]["usd"])


def _get_spot(asset: str):
    """Return cached spot price, refreshing if stale. Returns None on failure."""
    now = time.time()
    cached = _spot_cache.get(asset)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]
    try:
        price = _fetch_spot(asset)
        _spot_cache[asset] = (price, now)
        return price
    except Exception as e:
        logger.warning("[DIAMOND] spot fetch failed for %s: %s", asset, e)
        # Fall back to stale cache rather than hard-fail
        return cached[0] if cached else None


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _lognormal_prob(spot: float, strike: float, days: int, vol_daily: float) -> float:
    """
    P(price > strike at settlement) under log-normal with zero drift.
    Uses T = max(days, 1/24) to avoid div-by-zero on same-day settlement.
    """
    if spot <= 0 or strike <= 0:
        return 0.0
    T = max(days / 365.0, 1.0 / 8760)   # minimum 1 hour expressed in years
    vol_annual = vol_daily * math.sqrt(365)
    d = (math.log(spot / strike) - 0.5 * vol_annual**2 * T) / (vol_annual * math.sqrt(T))
    return _norm_cdf(d)


# ---------------------------------------------------------------------------
# DiamondAgent
# ---------------------------------------------------------------------------


class DiamondAgent(BaseAgent):
    """
    DIAMOND — crypto specialist.

    Evaluates Kalshi BTC/ETH price-level binary markets by comparing the
    log-normal fair-value probability against Kalshi's implied price.
    Fires signals only when the mispricing edge exceeds MIN_EDGE (10%).
    """

    name = "DIAMOND"
    domain = "crypto"
    seed_rules = [
        "Only trade BTC and ETH Kalshi price-level markets (KXBTCD/W, KXETHD/W)",
        "Only buy when log-normal edge exceeds 10% versus Kalshi implied price",
        "Volume must exceed 20000 — crypto markets can be thinly traded",
        "Never enter within 1 hour of settlement — price is near true value already",
        "If spot fetch fails, PASS — never trade blind without a fair-value model",
    ]

    # =========================================================================
    # should_evaluate — hot path, no I/O
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast pre-filter. Returns True only if market passes all guards.
        No I/O — must remain sub-millisecond.
        """
        if not self._base_should_evaluate(market):
            return False

        # Only crypto series
        if market.series_ticker not in _CRYPTO_SERIES:
            return False

        # Minimum liquidity — crypto markets can be thinly traded
        if market.volume_dollars < 1_000:
            return False

        # Spread too wide — market maker risk
        if market.spread > 0.06:
            return False

        # Price gates — 25¢–75¢ sweet spot (aligns with base_agent gate)
        if market.yes_price > 0.75:
            return False  # YES too expensive
        if market.yes_price < 0.25:
            return False  # NO too expensive (costs >75¢)

        return True

    # =========================================================================
    # evaluate — runs in daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Compute log-normal edge vs. Kalshi implied price.
        Submits HIGH_CONVICTION or PROPHECY signal if edge >= MIN_EDGE.
        """
        # 1. Parse ticker
        asset, strike = _parse_ticker(market.ticker)
        if asset is None:
            logger.debug("[DIAMOND] unparseable ticker: %s — skipping", market.ticker)
            return

        # 2. Fetch spot price
        spot = _get_spot(asset)
        if spot is None:
            logger.warning(
                "[DIAMOND] spot fetch failed for %s — PASS on %s",
                asset, market.ticker,
            )
            return

        # 3. Daily vol for asset
        vol_daily = _VOL_DAILY[asset]

        # 4. Compute fair-value probability P(close > strike)
        true_prob = _lognormal_prob(spot, strike, market.days_to_settlement, vol_daily)

        # 5. Directional edge: positive → YES is cheap, negative → NO is cheap
        directional_edge = (true_prob - market.yes_price) * 100
        edge_pct = abs(directional_edge)

        # 6. Skip if edge is too small
        if edge_pct < MIN_EDGE:
            logger.debug(
                "[DIAMOND] %s edge=%.1f%% below MIN_EDGE=%.1f%% — skip",
                market.ticker, edge_pct, MIN_EDGE,
            )
            return

        # 7. Determine side
        if directional_edge > 0:
            side = "yes"  # true_prob > implied → YES is underpriced
        else:
            side = "no"   # true_prob < implied → NO is underpriced

        # 8. Entry price
        if side == "yes":
            entry_price = round(market.yes_price, 4)
        else:
            entry_price = round(1.0 - market.yes_price, 4)

        # 9. Target price
        if side == "yes":
            target_price = round(min(0.95, true_prob + 0.05), 3)
        else:
            target_price = round(min(0.95, (1.0 - true_prob) + 0.05), 3)

        # 10. Stop price
        stop_price = round(max(0.05, entry_price - 0.10), 3)

        # 11. Conviction tier
        if edge_pct >= PROPHECY_EDGE:
            conviction_tier = "PROPHECY"
        else:
            conviction_tier = "HIGH_CONVICTION"

        # 12. Reasoning string
        reasoning = (
            f"Log-normal: spot={spot:,.0f} strike={strike:,.0f} "
            f"true_prob={true_prob:.1%} vs Kalshi={market.yes_price:.1%} "
            f"— {edge_pct:.1f}% edge ({asset})"
        )

        # 13. Build and submit signal
        signal = self.build_signal(
            market=market,
            conviction_tier=conviction_tier,
            edge_pct=edge_pct,
            side=side,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            reasoning=reasoning,
            game=game,
        )
        self.submit_signal(signal)
