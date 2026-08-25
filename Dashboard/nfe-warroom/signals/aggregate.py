"""Aggregator: pulls all live signals into one dict for scoring + dashboard."""
from signals import fred, crypto, coinglass, congress, macro_llm


def _get_wti_regime() -> dict:
    """WTI oil price regime signal.

    Placeholder until FRED DCOILWTICO or a live price feed is wired in.
    Returns oil_regime key consumed by scoring._macro_factor REGIME_MODIFIERS.

    Regimes: WAR_PREMIUM | HAWKISH_OIL | DOVISH_OIL | UNKNOWN
    """
    try:
        # TODO: Wire to FRED series DCOILWTICO or a free commodity API
        return {"oil_regime": "UNKNOWN", "oil_price": 0.0}
    except Exception:
        return {"oil_regime": "UNKNOWN", "oil_price": 0.0}


def snapshot():
    s = {}
    s.update(fred.get_all())
    s.update(crypto.get_all())
    s.update(coinglass.get_all())
    s.update(congress.get_all())
    s.update(macro_llm.get_all())
    s.update(_get_wti_regime())
    return s


def macro_table_rows(s: dict):
    """Return list of dicts shaped for the dashboard's macro table."""
    rows = []

    rows.append({
        "signal": "US-Iran",
        "status": s.get("us_iran_status", "UNKNOWN"),
        "source": "Claude intel",
        "impact": "Oil + / Gold + / JPY +" if s.get("us_iran_status") == "ACTIVE" else "—",
        "color": "r" if s.get("us_iran_status") == "ACTIVE" else "ts",
    })

    rows.append({
        "signal": "Strait of Hormuz",
        "status": s.get("hormuz_status", "UNKNOWN"),
        "source": "Claude intel",
        "impact": "Oil sustained premium" if s.get("hormuz_status") == "BLOCKED" else "—",
        "color": "r" if s.get("hormuz_status") == "BLOCKED" else "ts",
    })

    rows.append({
        "signal": "Fed Policy",
        "status": s.get("fed_status", "UNKNOWN"),
        "source": "FRED FFR",
        "impact": "Rates +macro / Equities -8 / Gold +4" if s.get("fed_status") == "HAWKISH" else "—",
        "color": "y" if s.get("fed_status") == "HAWKISH" else "ts",
    })

    rows.append({
        "signal": "BOJ / Yen",
        "status": s.get("boj_status", "UNKNOWN"),
        "source": "Claude intel",
        "impact": "USDJPY +16 macro" if "HIK" in str(s.get("boj_status", "")) else "—",
        "color": "y",
    })

    rows.append({
        "signal": "DXY Trend",
        "status": s.get("dxy_status", "UNKNOWN"),
        "source": "FRED (4hr)",
        "impact": "Rising = +8 forex / Commodities inverse",
        "color": "b" if s.get("dxy_status") == "RISING" else "ts",
    })

    rows.append({
        "signal": "Yield Curve",
        "status": s.get("curve_status", "UNKNOWN"),
        "source": "FRED 10y-2y (4hr)",
        "impact": "Inverted = +10 rates / +10 stocks",
        "color": "b",
    })

    rows.append({
        "signal": "Crypto Funding",
        "status": s.get("funding_status", "UNKNOWN"),
        "source": "Coinglass (15min)",
        "impact": "Negative = +20 / Very pos = -12",
        "color": "g" if "NEGATIVE" in str(s.get("funding_status", "")) else "ts",
    })

    fng_v = s.get("fng_value")
    rows.append({
        "signal": "BTC Fear & Greed",
        "status": s.get("fng_status", "UNKNOWN") + (f" ({fng_v})" if fng_v is not None else ""),
        "source": "alternative.me (1hr)",
        "impact": "Fear ≤20 = +15 / Greed ≥80 = -12",
        "color": "r" if "FEAR" in str(s.get("fng_status", "")) else "g" if "GREED" in str(s.get("fng_status", "")) else "ts",
    })

    rows.append({
        "signal": "Congress",
        "status": s.get("congress_status", "UNKNOWN"),
        "source": "House+Senate (2hr)",
        "impact": "Sector-weighted boost up to 25pts",
        "color": "g" if s.get("congress_status") not in (None, "UNKNOWN", "QUIET") else "ts",
    })

    rows.append({
        "signal": "Oil Priority Boost",
        "status": "+5 pts ACTIVE",
        "source": "Hard-coded",
        "impact": "Effective threshold = 55",
        "color": "cat-oil",
    })

    return rows
