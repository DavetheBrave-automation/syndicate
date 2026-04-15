"""
agents/oil.py — OIL agent: WTI crude oil specialist.

Domain: KXWTIW* and KXWTIMAX* Kalshi contracts (weekly/monthly WTI range bets)
Strategy: Macro regime + geopolitical premium overlay on Kalshi implied probability.
Iran war premium, Hormuz status, Fed/DXY regime are the primary drivers.

Key rules:
  - Edge minimum: 12% (wider than sports — oil is more volatile)
  - Never trade contracts >7 days to settlement
  - Macro gate: if Claude's oil_regime_score < 0, skip
  - WAR_PREMIUM regime: bias toward YES on higher strikes
  - Fed dovish + DXY falling = bullish oil setup (+5% edge confidence)
  - FNG < 30 = risk-off = selling pressure, bias NO on high strikes
  - Target 20% gain then exit — oil can reverse fast on news
"""

import logging

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.oil")

_OIL_SERIES_PREFIXES = ("KXWTIW", "KXWTIMAX", "KXWTI")
_MIN_EDGE_PCT        = 12.0
_MAX_DAYS            = 7


class OilAgent(BaseAgent):
    name       = "OIL"
    domain     = "commodities"

    seed_rules = [
        "Only trade KXWTIW and KXWTIMAX series",
        "Check macro_llm oil_regime_score before every trade — negative = skip",
        "Iran war premium: if oil_regime_score > 5, bias toward YES on higher strikes",
        "Strait of Hormuz status is the single most important factor — check overall_market_risk",
        "Never trade oil contracts settling more than 7 days out",
        "Edge must exceed 12% — oil is more volatile than sports, needs wider margin",
        "Fed dovish + DXY falling = bullish oil setup — add 5% to edge confidence",
        "Fear & Greed below 30 = risk-off = oil selling pressure, bias NO on high strikes",
        "Weekly range contracts: buy NO on the high end when geopolitical risk fades",
        "Target 20% gain then exit — oil can reverse fast on news",
    ]

    EVAL_COOLDOWN_SECONDS = 600  # 10 min — oil moves slower than tennis

    # =========================================================================
    # should_evaluate
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        # Only oil series tickers
        ticker_upper = market.ticker.upper()
        if not any(ticker_upper.startswith(p) for p in _OIL_SERIES_PREFIXES):
            # Also accept if series_ticker is oil
            if not any(market.series_ticker.upper().startswith(p) for p in _OIL_SERIES_PREFIXES):
                return False

        # Settlement window
        if market.days_to_settlement > _MAX_DAYS:
            return False

        return True

    # =========================================================================
    # evaluate
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        # Pull live signals — in-process cache keeps this fast
        try:
            from signals.aggregate import get_snapshot
            signals = get_snapshot()
        except Exception as e:
            logger.warning("[OIL] signals unavailable: %s — continuing without macro", e)
            signals = {}

        yes_price = market.yes_price  # 0.0–1.0

        # ── Price gate (same floor as all agents) ────────────────────────────
        if yes_price > 0.75 or yes_price < 0.25:
            return  # Outside tradeable range

        # ── Macro context — direction only, never a hard gate ────────────────
        oil_score   = float(signals.get("oil_regime_score", 0) or 0)
        dxy         = signals.get("dxy_status", "FLAT")    or "FLAT"
        fed         = signals.get("fed_status", "NEUTRAL") or "NEUTRAL"
        fng         = int(signals.get("fng_value", 50)     or 50)
        curve       = signals.get("curve_status", "NORMAL") or "NORMAL"
        market_risk = signals.get("overall_market_risk", "MEDIUM") or "MEDIUM"

        bullish_oil = (
            dxy == "FALLING"
            or fed == "DOVISH"
            or oil_score > 2
        )
        bearish_oil = (
            dxy == "RISING"
            or fed == "HAWKISH"
            or oil_score < -2
            or fng < 30
        )

        # ── Edge + direction from price position and macro ───────────────────
        if yes_price < 0.5 and bullish_oil:
            # Market underpricing YES — macro says oil going up
            side     = "yes"
            edge_pct = (0.5 - yes_price) * 100 + abs(oil_score) * 1.5

        elif yes_price > 0.5 and bearish_oil:
            # Market overpricing YES — macro says oil going down
            side     = "no"
            edge_pct = (yes_price - 0.5) * 100 + abs(oil_score) * 1.5

        elif yes_price < 0.4:
            # Deeply discounted YES — take it regardless of macro
            side     = "yes"
            edge_pct = (0.5 - yes_price) * 100

        elif yes_price > 0.6:
            # Deeply overpriced YES — fade it regardless of macro
            side     = "no"
            edge_pct = (yes_price - 0.5) * 100

        else:
            # 0.4–0.6 range with no clear macro direction — skip
            logger.debug("[OIL] No clear edge on %s (yes=%.2f, no macro bias)", market.ticker, yes_price)
            return

        # ── Minimum edge floor ───────────────────────────────────────────────
        if edge_pct < 8.0:
            logger.debug("[OIL] Edge %.1f%% below 8%% minimum on %s", edge_pct, market.ticker)
            return

        # ── Conviction tier ──────────────────────────────────────────────────
        if edge_pct >= 20 and abs(oil_score) > 4:
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        # ── Entry/target/stop ────────────────────────────────────────────────
        if side == "yes":
            entry_price  = yes_price
            target_price = round(min(0.90, yes_price + 0.15), 3)
            stop_price   = round(max(0.05, yes_price - 0.10), 3)
        else:
            entry_price  = yes_price  # build_signal uses YES price for NO trades
            target_price = round(max(0.10, yes_price - 0.15), 3)
            stop_price   = round(min(0.95, yes_price + 0.10), 3)

        direction_label = "BULLISH" if bullish_oil else "BEARISH" if bearish_oil else "NEUTRAL"

        reasoning = (
            f"OIL {side.upper()} | edge={edge_pct:.1f}% | "
            f"yes_price={yes_price:.2f} | oil_score={oil_score:+.1f}\n"
            f"DXY={dxy} FED={fed} FNG={fng} CURVE={curve}\n"
            f"Direction: {direction_label} oil\n"
            f"TC: confirm this WTI contract has real edge. "
            f"Check current WTI spot price and whether {side.upper()} at "
            f"{yes_price:.0%} makes sense given Iran war status and Hormuz situation."
        )

        signal = self.build_signal(
            market          = market,
            conviction_tier = conviction_tier,
            edge_pct        = edge_pct,
            side            = side,
            entry_price     = entry_price,
            target_price    = target_price,
            stop_price      = stop_price,
            reasoning       = reasoning,
            game            = None,
        )
        if signal is None:
            return
        self.submit_signal(signal)
