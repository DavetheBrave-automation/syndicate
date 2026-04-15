"""
delta.py — DELTA Arbitrage Agent.

Strategy: Find contracts where Kalshi price differs significantly from external consensus.

For KXBTCD (BTC price) markets: pre-fetches live BTC spot from Coinbase at signal time
and embeds it in the signal. TC reads the embedded data — no web search needed.

For other series (politics, sports, etc.): still requires TC web search to validate.

Rules:
  - Minimum 8% gap between Kalshi and external consensus required
  - Never trade if external consensus is older than 48 hours
  - Volume > 5000 (arb only works in liquid markets)
  - Exit when gap closes to 2%
  - BTC: fetch spot from Coinbase, compare to strike, compute gap self-contained
  - YES side: buy when Kalshi underprices an outcome vs external consensus
  - NO side: buy when Kalshi overprices an outcome vs external consensus
  - Never buy NO above 80¢ YES — too expensive for arb play
"""

import os
import sys
import logging

import requests

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.delta")

_MIN_VOLUME          = 5_000
_MIN_DAYS            = 0.04  # don't arb contracts expiring in <1 hour
_MAX_DAYS            = 30    # only trade where external data is available
_MIN_EDGE_PCT        = 15.0   # Override base class 7.0 — DELTA fires on everything at exactly 10%
_PROPHECY_EDGE_PCT   = 20.0

# Series where external consensus data exists
_ARB_ELIGIBLE_PREFIXES = {
    "KXPOL", "KXECON", "KXFED", "KXCPI", "KXELEC", "KXAPPROVAL",  # politics/econ
    "KXNBA", "KXMLB", "KXNHL", "KXNFL", "KXPGATOUR",               # major sports
    "KXBTCD", "KXETHUSD",                                            # crypto
}


def _get_btc_spot() -> float | None:
    """Fetch live BTC/USD spot price from Coinbase. Embedded in signal so TC needs no web search."""
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5,
        )
        return float(r.json()["data"]["amount"])
    except Exception:
        return None


def _parse_btc_strike(ticker: str) -> float | None:
    """Extract strike price from KXBTCD ticker. e.g. 'KXBTCD-26APR1617-T74999.99' → 74999.99"""
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        return None


def _extract_series(ticker: str) -> str:
    return ticker.split("-")[0].upper() if ticker else ""


def _build_search_query(market) -> str:
    """Build a targeted search query based on the market ticker and title."""
    series = _extract_series(market.ticker)
    # Map series to meaningful search context
    _QUERY_MAP = {
        "KXBTCD":    f"Bitcoin price prediction today probability",
        "KXETHUSD":  f"Ethereum price forecast probability",
        "KXFED":     f"Federal Reserve rate decision probability prediction market consensus",
        "KXCPI":     f"CPI inflation forecast Bloomberg consensus",
        "KXNBA":     f"NBA game win probability tonight odds",
        "KXMLB":     f"MLB game moneyline odds probability",
        "KXNFL":     f"NFL game win probability odds",
        "KXNHL":     f"NHL game win probability odds",
        "KXPGATOUR": f"PGA Tour winner odds probability",
    }
    base = _QUERY_MAP.get(series, f"prediction market probability {market.ticker}")
    return f"{base} vs Kalshi {round(market.yes_price * 100):.0f}¢ — find external consensus"


class DeltaAgent(BaseAgent):
    name                  = "DELTA"
    domain                = "all"
    MAX_SIGNALS_PER_CYCLE = 3   # Top 3 by edge_pct — prevents BTC ladder spam

    seed_rules = [
        "Find contracts where Kalshi price differs significantly from external consensus",
        "For sports: compare Kalshi implied probability to Vegas moneyline converted to probability",
        "For politics: compare Kalshi to PredictIt and Polymarket prices via TC web search",
        "Minimum 8% gap between Kalshi and external consensus required",
        "Never trade if external consensus is older than 48 hours",
        "Kalshi lags PredictIt by 3-5 percentage points on political markets — factor this in",
        "Tennis: compare to ATP live win probability tools when available",
        "Crypto: compare to options implied volatility for pricing sanity check",
        "Exit when gap closes to 2% — arbitrage is complete",
        "BTC markets: spot price pre-fetched from Coinbase — no web search needed, trust embedded data",
        "Other markets: TC web search still required to validate gap",
        "Volume must exceed $5000 — arb only works in liquid markets",
        "YES side: buy when Kalshi underprices vs external consensus",
        "NO side: buy when Kalshi overprices vs external consensus",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast gate:
          - Market must be in an arb-eligible series (external data exists)
          - Minimum volume for arbitrage to be profitable
          - Not too close to settlement (arb needs time to close)
          - YES price not in ghost zone (5¢ or below)
        """
        if not self._base_should_evaluate(market):
            return False

        if market.volume_dollars < _MIN_VOLUME:
            return False

        if market.days_to_settlement < _MIN_DAYS:
            return False

        if market.days_to_settlement > _MAX_DAYS:
            return False

        # Lottery ticket filter: multi-day contracts only worth trading as real long shots.
        # yes_price > 10¢ at 2+ days out → not a long shot, skip.
        if market.days_to_settlement > 1 and market.yes_price > 0.10:
            return False

        # Must be in a series with available external data
        series = _extract_series(market.ticker)
        if not any(market.ticker.upper().startswith(p) for p in _ARB_ELIGIBLE_PREFIXES):
            return False

        # Avoid ghost zone — no arb value at extremes
        if market.yes_price < 0.05 or market.yes_price > 0.95:
            return False

        # Avoid NO above 80¢ YES (too expensive for arb)
        if market.yes_price > 0.80:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Build an arb signal.

        For KXBTCD series: pre-fetch BTC spot from Coinbase, compute gap directly.
        All data embedded in signal — TC needs NO web search.

        For other series: signal includes search query; TC does web search to validate.

        YES side: Kalshi UNDERPRICES (buy YES → profits when market moves up)
        NO side:  Kalshi OVERPRICES  (buy NO  → profits when market moves down)
        """
        series    = _extract_series(market.ticker)
        yes_price = market.yes_price
        no_price  = round(1.0 - yes_price, 4)

        # ── BTC SERIES: self-contained arb with embedded live price ────────────
        if market.ticker.upper().startswith("KXBTCD"):
            btc_spot = _get_btc_spot()
            if btc_spot is None:
                logger.warning("[DELTA] BTC spot fetch failed for %s — skipping", market.ticker)
                return

            strike = _parse_btc_strike(market.ticker)
            if strike is None:
                logger.warning("[DELTA] Could not parse strike from %s — skipping", market.ticker)
                return

            # Binary true probability: BTC above strike → YES should win (p≈1.0)
            #                          BTC below strike → NO  should win (p≈0.0)
            true_prob = 1.0 if btc_spot > strike else 0.0
            gap       = abs(yes_price - true_prob)

            if gap < 0.08:
                logger.debug(
                    "[DELTA] Gap too small: %s btc=$%.0f strike=$%.0f gap=%.0f%%",
                    market.ticker, btc_spot, strike, gap * 100,
                )
                return

            # Direction: buy the side that's currently mispriced
            if btc_spot > strike:
                side = "yes"   # BTC above → YES should win → Kalshi underpriced YES
            else:
                side = "no"    # BTC below → NO should win  → Kalshi underpriced NO

            edge_pct        = round(gap * 100, 2)
            conviction_tier = "PROPHECY" if gap >= 0.20 else "HIGH_CONVICTION"

            # entry_price convention: always YES price in 0..1
            entry_price  = round(yes_price, 4)
            our_cost     = round(yes_price if side == "yes" else no_price, 4)
            target_price = round(min(0.95, our_cost + gap * 0.5), 4)
            stop_price   = round(max(0.05, our_cost - 0.10), 4)

            reasoning = (
                f"DELTA ARB | gap={gap:.0%} | "
                f"btc_spot=${btc_spot:,.0f} | strike=${strike:,.0f} | "
                f"btc_{'above' if btc_spot > strike else 'below'}_strike | "
                f"kalshi_yes={yes_price:.0%} | true_prob={true_prob:.0%} | "
                f"correct_side={side} | edge={edge_pct:.1f}% | "
                f"self_contained=True — no web search needed"
            )

            signal = self.build_signal(
                market, conviction_tier, edge_pct, side,
                entry_price, target_price, stop_price, reasoning, game,
            )
            if signal is None:
                return

            # Embed computed arb data directly — TC reads this, no web search
            sig = signal["signal"]
            sig["self_contained"]    = True
            sig["requires_web_search"] = False
            sig["btc_spot"]          = btc_spot
            sig["strike"]            = strike
            sig["gap"]               = round(gap, 4)
            sig["true_prob"]         = true_prob
            sig["arb_series"]        = series
            sig["exit_trigger"]      = "gap closes to ≤ 2% or our side ≥ 85¢"

            if market.days_to_settlement > 1:
                sig["max_size_dollars"] = 2
                sig["lottery_ticket"]   = True

            logger.info(
                "[DELTA] BTC arb signal: %s side=%s YES=%d¢ btc=$%.0f strike=$%.0f gap=%.0f%%",
                market.ticker, side, int(yes_price * 100), btc_spot, strike, gap * 100,
            )
            self.submit_signal(signal)
            return

        # ── ALL OTHER SERIES: web-search flow (unchanged) ──────────────────────
        search_q        = _build_search_query(market)
        conviction_tier = "HIGH_CONVICTION"
        edge_pct_est    = _MIN_EDGE_PCT + 2.0

        if yes_price < 0.50:
            side         = "yes"
            entry_price  = round(yes_price, 4)
            target_price = round(min(0.90, yes_price * 1.20), 4)
            stop_price   = round(max(0.05, yes_price * 0.80), 4)
        else:
            side         = "no"
            entry_price  = round(yes_price, 4)
            target_price = round(max(0.10, yes_price * 0.80), 4)
            stop_price   = round(min(0.95, yes_price * 1.20), 4)

        reasoning = (
            f"DELTA: Potential arb on {market.ticker}. "
            f"Kalshi YES={yes_price:.2f} ({yes_price*100:.0f}¢). "
            f"Requires TC web search: [{search_q}] "
            f"Execute if gap ≥ 8%."
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct_est, side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        if signal is None:
            return

        if market.days_to_settlement > 1:
            signal["signal"]["max_size_dollars"] = 2
            signal["signal"]["lottery_ticket"]   = True

        signal["signal"]["requires_web_search"] = True
        signal["signal"]["search_query"]         = search_q
        signal["signal"]["arb_series"]           = series
        signal["signal"]["min_gap_required"]     = 0.08

        self.submit_signal(signal)
        logger.info(
            "[DELTA] Arb signal (web): %s YES=%d¢ | search: %s",
            market.ticker, int(yes_price * 100), search_q[:60],
        )
