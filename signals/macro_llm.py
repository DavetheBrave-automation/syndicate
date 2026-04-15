"""
signals/macro_llm.py — Macro regime scoring derived from FRED + FNG signals.
No external API call. Uses live signal data to compute regime scores.
Claude reasoning happens inside TC gate when OIL/ORACLE agents submit signals.
Cache: 5 min (refreshes with heartbeat cycle).
"""
import os
import json
import time
import logging

logger = logging.getLogger("syndicate.signals.macro_llm")

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_SYNDICATE_ROOT, "signals", "cache", "macro_llm.json")
_CACHE_SEC  = 300   # 5 min — fast enough to track intraday shifts


def get_all() -> dict:
    # Cache hit
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < _CACHE_SEC:
                return {k: v for k, v in cached.items() if not k.startswith("_")}
    except Exception:
        pass

    # Pull live FRED + FNG — each has its own cache so no double-fetch
    from signals.fred import get_all as fred_all
    from signals.fng  import get_all as fng_all

    fred = fred_all()
    fng  = fng_all()

    fng_val = int(fng.get("fng_value", 50) or 50)
    fng_lbl = fng.get("fng_status", "NEUTRAL") or "NEUTRAL"
    fed     = fred.get("fed_status", "NEUTRAL") or "NEUTRAL"
    dxy     = fred.get("dxy_status", "FLAT")    or "FLAT"
    curve   = fred.get("curve_status", "NORMAL") or "NORMAL"

    # ── Oil regime ──────────────────────────────────────────────────────────
    oil_score = 0.0
    if dxy  == "FALLING": oil_score += 4.0   # weak dollar → higher oil
    if dxy  == "RISING":  oil_score -= 3.0   # strong dollar → suppresses oil
    if fed  == "DOVISH":  oil_score += 3.0   # easy money → demand/inflation
    if fed  == "HAWKISH": oil_score -= 2.0   # tightening → demand headwind
    if fng_val < 25:      oil_score -= 2.0   # risk-off → oil selling
    if fng_val > 75:      oil_score += 2.0   # risk-on  → commodity demand

    # ── Crypto regime ───────────────────────────────────────────────────────
    crypto_score = 0.0
    if fng_val < 20:      crypto_score += 8.0   # extreme fear = contrarian buy
    if fng_val > 80:      crypto_score -= 6.0   # extreme greed = fade
    if fed  == "DOVISH":  crypto_score += 4.0
    if fed  == "HAWKISH": crypto_score -= 3.0

    # ── Rates regime ────────────────────────────────────────────────────────
    rates_score = 0.0
    if curve == "INVERTED": rates_score += 6.0   # inversion = high signal clarity
    if fed == "HAWKISH":    rates_score += 4.0
    if fed == "DOVISH":     rates_score -= 2.0

    # ── Sports regime ───────────────────────────────────────────────────────
    # ESPN/schedule signals drive this — macro is irrelevant; leave neutral
    sports_score = 0.0

    # ── Overall risk level ──────────────────────────────────────────────────
    if fng_val < 20 or curve == "INVERTED":
        risk = "HIGH"
    elif fng_val < 35 or dxy == "RISING":
        risk = "MEDIUM"
    elif fng_val > 75:
        risk = "MEDIUM"   # complacency = hidden risk
    else:
        risk = "LOW"

    # ── Top opportunity class ───────────────────────────────────────────────
    scores = {
        "oil":    oil_score,
        "crypto": crypto_score,
        "rates":  rates_score,
        "sports": sports_score,
    }
    top_class = max(scores, key=lambda k: scores[k])

    result = {
        "oil_regime_score":    round(oil_score,    1),
        "crypto_regime_score": round(crypto_score, 1),
        "rates_regime_score":  round(rates_score,  1),
        "sports_regime_score": round(sports_score, 1),
        "overall_market_risk":   risk,
        "top_opportunity_class": top_class,
        "macro_llm_status":    "DERIVED",   # no API call — FRED+FNG derived
        "oil_narrative":    f"Fed {fed}, DXY {dxy}, score {oil_score:+.1f}",
        "crypto_narrative": f"F&G {fng_val} ({fng_lbl}), score {crypto_score:+.1f}",
        "rates_narrative":  f"Curve {curve}, Fed {fed}, score {rates_score:+.1f}",
        "sports_narrative": "Neutral — ESPN signals drive sports",
    }

    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({**result, "_ts": time.time()}, f)
    except Exception as e:
        logger.warning("[MacroLLM] cache write failed: %s", e)

    logger.info(
        "[MacroLLM] Derived: oil=%+.1f crypto=%+.1f rates=%+.1f risk=%s top=%s",
        oil_score, crypto_score, rates_score, risk, top_class,
    )
    return result
