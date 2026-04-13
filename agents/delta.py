"""
delta.py — DELTA Arbitrage Agent.

Strategy: Find contracts where Kalshi price differs significantly from external consensus.
DELTA always requires TC web search — never evaluates without current external data.

Rules:
  - Minimum 8% gap between Kalshi and external consensus required
  - Never trade if external consensus is older than 48 hours
  - Kalshi lags PredictIt by 3-5 pp on political markets — factor this in
  - Tennis: compare to ATP live win probability when available
  - Volume > 5000 (arb only works in liquid markets)
  - Exit when gap closes to 2%
  - This agent requires TC web search — never evaluate without it
  - YES side: buy when Kalshi underprices an outcome vs external consensus
  - NO side: buy when Kalshi overprices an outcome vs external consensus
  - Never buy NO above 80¢ YES — too expensive for arb play
"""

import os
import sys
import logging

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
        "This agent requires TC web search — never evaluate without it",
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
        Build a signal that tells TC to web-search for external consensus price.
        TC computes the actual gap and decides whether to execute.

        YES side: buy when Kalshi UNDERPRICES the outcome vs external consensus
        NO side:  buy when Kalshi OVERPRICES the outcome vs external consensus
        Never buy NO above 80¢ YES (NO costs < 20¢ — needs huge move to profit).
        """
        series     = _extract_series(market.ticker)
        yes_price  = market.yes_price
        no_price   = round(1.0 - yes_price, 4)
        search_q   = _build_search_query(market)

        # DELTA submits signals at HIGH_CONVICTION by default;
        # TC upgrades to PROPHECY if gap > 20% after web search.
        conviction_tier = "HIGH_CONVICTION"
        edge_pct_est    = _MIN_EDGE_PCT + 2.0  # TC will compute actual after search

        # Entry parameters — TC will refine based on search results
        if yes_price < 0.50:
            # Could be YES (underpriced) or NO (correctly priced) — TC decides after search
            side         = "yes"
            entry_price  = round(yes_price, 4)
            target_price = round(min(0.90, yes_price * 1.20), 4)
            stop_price   = round(max(0.05, yes_price * 0.80), 4)
        else:
            # Could be NO (overpriced) or YES (correctly priced) — TC decides after search
            side         = "no"
            entry_price  = round(yes_price, 4)
            target_price = round(max(0.10, yes_price * 0.80), 4)
            stop_price   = round(min(0.95, yes_price * 1.20), 4)

        reasoning = (
            f"DELTA: Potential arb on {market.ticker}. "
            f"Kalshi YES={yes_price:.2f} ({yes_price*100:.0f}¢), NO={no_price:.2f} ({no_price*100:.0f}¢). "
            f"Requires TC web search: [{search_q}] "
            f"Calculate gap vs external consensus. "
            f"Execute if gap ≥ 8%. PROPHECY if gap ≥ 20%. "
            f"Exit trigger: gap closes to ≤ 2%. [web_search=True]"
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct_est, side,
            entry_price, target_price, stop_price, reasoning, game,
        )

        # Lottery ticket size cap: multi-day long shots hard-capped at $2
        if market.days_to_settlement > 1:
            signal["signal"]["max_size_dollars"] = 2
            signal["signal"]["lottery_ticket"]   = True

        # Tag for TC: web search is mandatory
        signal["signal"]["requires_web_search"] = True
        signal["signal"]["search_query"]         = search_q
        signal["signal"]["arb_series"]           = series
        signal["signal"]["kalshi_yes_price"]     = yes_price
        signal["signal"]["kalshi_no_price"]      = no_price
        signal["signal"]["min_gap_required"]     = 0.08
        signal["signal"]["prophecy_gap"]         = 0.20
        signal["signal"]["exit_trigger"]         = "gap closes to ≤ 2%"

        self.submit_signal(signal)
        logger.info(
            "[DELTA] Arb signal: %s YES=%d¢ | search: %s",
            market.ticker, int(yes_price * 100), search_q[:60],
        )
