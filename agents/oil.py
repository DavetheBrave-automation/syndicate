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
            logger.warning("[OIL] signals unavailable: %s — skipping", e)
            return

        oil_score   = float(signals.get("oil_regime_score", 0) or 0)
        market_risk = signals.get("overall_market_risk", "MEDIUM")
        fng         = int(signals.get("fng_value", 50) or 50)
        fed         = signals.get("fed_status", "NEUTRAL") or "NEUTRAL"
        dxy         = signals.get("dxy_status", "FLAT") or "FLAT"

        # Hard macro gate — negative oil score = no edge in current regime
        if oil_score < 0:
            logger.debug("[OIL] Skipping %s — oil_regime_score=%.1f (negative)", market.ticker, oil_score)
            return

        yes_price = market.yes_price   # 0.0–1.0
        no_price  = round(1.0 - yes_price, 4)

        # Directional bias from macro regime
        macro_bias_up = (
            oil_score > 3
            or market_risk in ("HIGH", "EXTREME")
            or (fed == "DOVISH" and dxy == "FALLING")
        )
        macro_bias_down = (
            fng < 30 and market_risk == "LOW"
        ) or oil_score < -2

        if yes_price < 0.5 and macro_bias_up:
            # YES underpriced relative to macro regime
            side     = "yes"
            # Edge = distance from 50¢ + macro premium
            edge_pct = (0.5 - yes_price) * 100 + abs(oil_score) * 2
        elif yes_price > 0.5 and macro_bias_down:
            # NO underpriced — crowd overconfident on YES
            side     = "no"
            edge_pct = (yes_price - 0.5) * 100 + abs(oil_score) * 2
        elif yes_price < 0.5 and oil_score >= 0:
            # Weak bullish — neutral edge from price displacement only
            side     = "yes"
            edge_pct = (0.5 - yes_price) * 80  # reduced confidence
        else:
            logger.debug("[OIL] No clear edge on %s (yes=%.2f oil_score=%.1f)", market.ticker, yes_price, oil_score)
            return

        if edge_pct < _MIN_EDGE_PCT:
            logger.debug("[OIL] Edge %.1f%% below %.1f%% minimum on %s", edge_pct, _MIN_EDGE_PCT, market.ticker)
            return

        # Conviction tier
        if edge_pct >= 25 or (oil_score > 7 and market_risk == "EXTREME"):
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        # Prices and levels
        if side == "yes":
            entry_price  = yes_price
            target_price = round(min(0.90, yes_price + 0.15), 3)
            stop_price   = round(max(0.05, yes_price - 0.10), 3)
        else:
            entry_price  = yes_price   # base_agent stores YES price for NO trades
            target_price = round(max(0.10, yes_price - 0.15), 3)
            stop_price   = round(min(0.95, yes_price + 0.10), 3)

        reasoning = (
            f"OIL AGENT — {side.upper()} opportunity\n"
            f"Edge: {edge_pct:.1f}% | Conviction: {'HIGH' if edge_pct > 20 else 'STANDARD'}\n"
            f"Macro: oil_score={oil_score:+.1f} | risk={market_risk}\n"
            f"Fed: {fed} | DXY: {dxy} | "
            f"Curve: {signals.get('curve_status', '—')}\n"
            f"F&G: {fng} ({signals.get('fng_status', '—')})\n"
            f"Oil narrative: {signals.get('oil_narrative', '—')}\n"
            f"TC: evaluate this oil contract against current geopolitical context "
            f"(Iran war, Hormuz, OPEC) and confirm YES/NO edge."
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
        self.submit_signal(signal)
