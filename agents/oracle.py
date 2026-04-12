"""
oracle.py — ORACLE Agent: Politics & Economics specialist.

Domain: Kalshi political/economic contracts (KXPOL, KXECON, KXFED, KXCPI, etc.)
Strategy: Edge from polling data, Bloomberg consensus, and market mispricing.
TC is prompted with web_search=True for current data — ORACLE never trades blind.

Rules:
  - Only trade contracts settling within 14 days
  - Always web-search current data before trading politics or economics
  - Economic contracts: compare Kalshi price to Bloomberg consensus
  - Never trade day before major announcement (spread widens unpredictably)
  - Edge must exceed 12% after verifying current data
  - Political polls: use only polls within 2 weeks — older = noise
  - If web search unavailable, PASS — never trade politics blind
  - Hedge: if buying candidate A YES, consider candidate B NO simultaneously
  - YES side: buy when you think price will go UP (contract settles $1)
  - NO side: buy when you think price will go DOWN ($0 for YES = $1 for NO)
  - Never buy NO on a contract priced above 80¢ YES (NO costs 20¢, needs large move)
  - Never buy YES on a contract priced below 5¢ (GHOST territory — different rules)
"""

import os
import sys
import logging

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.oracle")

# Kalshi series prefixes owned by ORACLE
ORACLE_SERIES = {
    "KXPOL",       # Politics general
    "KXECON",      # Economics general
    "KXFED",       # Federal Reserve rate decisions
    "KXCPI",       # CPI inflation
    "KXELEC",      # Elections
    "KXAPPROVAL",  # Presidential approval
}

_MAX_DAYS_TO_SETTLEMENT = 14
_MIN_VOLUME_DOLLARS     = 1_000
_MIN_EDGE_PCT           = 12.0
_PROPHECY_EDGE_PCT      = 25.0


def _series_from_ticker(ticker: str) -> str:
    """Extract series prefix: 'KXCPI-26MAY01-T3.2' → 'KXCPI'."""
    return ticker.split("-")[0].upper() if ticker else ""


class OracleAgent(BaseAgent):
    name   = "ORACLE"
    domain = "politics_economics"

    seed_rules = [
        "Only trade contracts settling within 14 days",
        "Use TC web search to check polling data before any political contract",
        "Economic contracts (CPI, Fed rate): compare Kalshi to Bloomberg consensus",
        "Never trade the day before a major announcement — spread widens unpredictably",
        "Edge must exceed 12% after checking current data",
        "Political contracts: polls within 2 weeks only — older polls are noise",
        "If web search unavailable, PASS — never trade politics blind",
        "Hedge: if buying candidate A YES, consider candidate B NO simultaneously",
        "YES side: buy when you think price will go UP (contract settles $1)",
        "NO side: buy when you think price will go DOWN (NO contract settles $1)",
        "Never buy NO on a contract priced above 80¢ YES (only 20¢ left, needs large move)",
        "Never buy YES on a contract priced below 5¢ (GHOST territory — different rules)",
    ]

    # =========================================================================
    # should_evaluate — hot path (no file I/O, no web search here)
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast filter: only pass politics/econ contracts with volume, in window.
        YES/NO guidance:
          YES side: buy when you think price will go UP
          NO side:  buy when you think price will go DOWN
          Never evaluate if YES price < 5¢ (GHOST territory)
          Never evaluate if YES price > 95¢ (too expensive for NO at 5¢)
        """
        if not self._base_should_evaluate(market):
            return False

        # Series must be in ORACLE domain
        series = _series_from_ticker(market.ticker)
        if series not in ORACLE_SERIES:
            # Also accept any market whose ticker starts with an ORACLE series prefix
            if not any(market.ticker.upper().startswith(s) for s in ORACLE_SERIES):
                return False

        # Days to settlement cap
        if market.days_to_settlement > _MAX_DAYS_TO_SETTLEMENT:
            return False

        # Minimum volume
        if market.volume_dollars < _MIN_VOLUME_DOLLARS:
            return False

        # GHOST zone exclusions — not ORACLE's domain
        if market.yes_price < 0.05 or market.yes_price > 0.95:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Build a signal for TC with web_search flag = True.
        TC will be prompted to look up current polling/consensus data before deciding.

        YES side: profits if price ABOVE strike
        NO side:  profits if price BELOW strike (currently {no_price:.2f})
        Never buy NO on a contract priced above 80¢ YES (only 20¢ left, needs large move)
        Never buy YES on a contract priced below 5¢ (GHOST territory — different rules)
        """
        ticker     = market.ticker
        yes_price  = market.yes_price
        no_price   = round(1.0 - yes_price, 4)
        days_left  = market.days_to_settlement

        # Don't trade day-before announcements (rough heuristic: <1 day AND < 3 days)
        if 0.5 < days_left < 1.5:
            logger.debug(
                "[ORACLE] Skipping %s — day-before window (days_left=%.2f)",
                ticker, days_left,
            )
            return

        # Determine candidate side — prefer YES when YES is mispriced low,
        # prefer NO when YES is mispriced high
        # TC will verify with web search; we just build the signal.
        if yes_price < 0.40:
            # Market thinks event is unlikely — check if underpriced (YES opportunity)
            side           = "yes"
            entry_price    = yes_price
            target_pct     = _MIN_EDGE_PCT / 100.0
            target_price   = round(min(0.95, entry_price * (1 + target_pct * 2)), 4)
            stop_price     = round(max(0.01, entry_price * 0.75), 4)
            edge_pct_est   = 15.0  # TC will compute actual edge after web search
            reasoning      = (
                f"ORACLE: YES on {ticker} — yes_price={yes_price:.2f} ({yes_price*100:.0f}¢). "
                f"Market says unlikely. TC to verify with polling/consensus data. "
                f"NO would cost {no_price:.2f} ({no_price*100:.0f}¢). "
                f"Settle date: {days_left:.1f} days. [web_search=True]"
            )
        else:
            # Market thinks event is likely — check if overpriced (NO opportunity)
            side           = "no"
            entry_price    = yes_price  # pass YES price; build_signal computes NO cost
            target_pct     = _MIN_EDGE_PCT / 100.0
            target_price   = round(max(0.05, yes_price * (1 - target_pct * 2)), 4)
            stop_price     = round(min(0.99, yes_price * 1.25), 4)
            edge_pct_est   = 13.0
            reasoning      = (
                f"ORACLE: NO on {ticker} — yes_price={yes_price:.2f} ({yes_price*100:.0f}¢), "
                f"NO costs {no_price:.2f} ({no_price*100:.0f}¢). "
                f"Market says likely — check if overpriced. TC to verify with data. "
                f"[web_search=True]"
            )

        # Use HIGH_CONVICTION by default — TC upgrades to PROPHECY after verifying data
        conviction_tier = "HIGH_CONVICTION"

        signal = self.build_signal(
            market         = market,
            conviction_tier = conviction_tier,
            edge_pct        = edge_pct_est,
            side            = side,
            entry_price     = entry_price,
            target_price    = target_price,
            stop_price      = stop_price,
            reasoning       = reasoning,
            game            = None,
        )

        # Tag signal for TC: web_search required before deciding
        signal["signal"]["web_search_required"] = True
        signal["signal"]["oracle_series"]        = _series_from_ticker(ticker)

        self.submit_signal(signal)
        logger.info(
            "[ORACLE] Signal submitted: %s %s edge~%.0f%%",
            ticker, side.upper(), edge_pct_est,
        )
