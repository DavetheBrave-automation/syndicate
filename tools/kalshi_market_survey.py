"""
Kalshi Market Survey — Read-only scan: Commodities / Economics / Companies / Financials / Tech / Science.
Uses Syndicate's RSA-PSS kalshi_rest connector. NO trades placed.
Output: audits/2026-04-20_kalshi_market_survey.md
"""

import os
import sys
import time
import math
import re
from datetime import datetime, timezone

SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SYNDICATE_ROOT)

# Use Syndicate's authenticated REST connector
from connectors.kalshi_rest import _get  # read-only; no orders

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ---------------------------------------------------------------------------
# Series probe list — organized by target category
# ---------------------------------------------------------------------------

CATEGORY_SERIES = {
    "Commodities": [
        "KXWTI", "KXWTIOIL", "KXOIL", "KXBRENT", "KXCRUD", "KXCRUDE",
        "KXNG", "KXNATGAS", "KXNGAS",
        "KXGOLD", "KXGOLDPRICE",
        "KXSILVER",
        "KXCOPPER",
        "KXCORN", "KXWHEAT", "KXSOY", "KXSOYBEANS",
        "KXCOMMOD",
    ],
    "Economics": [
        "KXFED", "KXFOMC", "KXFFR", "KXFEDRATE",
        "KXCPI", "KXINFLATION",
        "KXPCE",
        "KXPPI",
        "KXGDP",
        "KXUNEM", "KXUNEMPLOYMENT",
        "KXJOBLESS", "KXJOBLESSCLAIMS", "KXUI",
        "KXNFP", "KXPAYROLL",
        "KXRETAIL", "KXRETAILSALES",
        "KXISM", "KXPMI",
        "KXHOUSING",
        "KXECON",
    ],
    "Companies": [
        "KXAAPL", "KXMSFT", "KXNVDA", "KXTSLA", "KXAMZN",
        "KXGOOGL", "KXGOOGLE", "KXMETA", "KXNFLX", "KXAMD",
        "KXINTC", "KXINTEL",
        "KXEARNINGS", "KXEARNING", "KXEPS",
        "KXSTOCKS", "KXCORPEVENTS",
    ],
    "Financials": [
        "KXSP500", "KXSPX", "KXSNP", "INXD", "KXINXD",
        "KXNDX", "KXNASD", "KXNASDAQ",
        "KXDJI", "KXDOW", "KXDJIA",
        "KXVIX",
        "KXDXY", "KXDOLLAR",
        "KXTNOTE", "KXTYIELD", "KXBOND", "KXYIELD",
        "KXSPY", "KXQQQ",
        "KXFINANCIALS",
    ],
    "Tech": [
        "KXAI", "KXAIMODEL", "KXCHATGPT", "KXOPENAI", "KXGPT",
        "KXTECH",
        "KXCRYPTO", "KXBTCD", "KXETHD", "KXBTC", "KXETH",
    ],
    "Science": [
        "KXHURRICANE", "KXHURR",
        "KXWEATHER", "KXTEMP", "KXCLIMATE",
        "KXFDA", "KXDRUG", "KXVACCINE", "KXMEDICAL",
        "KXSPACE", "KXNASA", "KXSPACEX",
        "KXCOVID", "KXFLU",
        "KXSCIENCE",
    ],
}

# Also do a global sweep of first N pages to catch anything missed
GLOBAL_SWEEP_PAGES = 8  # 200 markets/page = up to 1600 markets from global sweep

# ---------------------------------------------------------------------------
# Classify markets that come from the global sweep
# ---------------------------------------------------------------------------

CATEGORY_TITLE_RULES = [
    ("Commodities", r"\b(crude oil|WTI|Brent|natural gas|gold price|silver|copper|corn|wheat|soybean|oil barrel|commodity)\b"),
    ("Economics", r"\b(federal funds rate|fed rate|interest rate|basis point|CPI|inflation|GDP|unemployment rate|jobless claims|retail sales|PCE|PPI|nonfarm payroll|ISM|PMI|housing starts|FOMC|rate hike|rate cut|core inflation)\b"),
    ("Companies", r"\b(earnings per share|EPS|quarterly earnings|revenue beat|stock price|shares outstanding|market cap|IPO|Q[1-4] 20\d\d earnings)\b"),
    ("Financials", r"\b(S&P 500|Nasdaq|Dow Jones|10-year yield|2-year yield|treasury yield|DXY|dollar index|VIX|DJIA|stock market|index level|SPX)\b"),
    ("Tech", r"\b(artificial intelligence|AI model|ChatGPT|GPT-\d|Claude \d|Gemini|Llama|hyperscaler|chip|semiconductor|crypto|bitcoin|ethereum|blockchain|model release|benchmark score)\b"),
    ("Science", r"\b(hurricane|tropical storm|temperature record|climate|FDA approval|drug approved|vaccine|clinical trial|space launch|rocket|NASA|SpaceX|COVID|flu pandemic)\b"),
]

def classify_by_title(title: str) -> str | None:
    for category, pattern in CATEGORY_TITLE_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return category
    return None

# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def fetch_series(series: str, max_pages: int = 5) -> list:
    markets = []
    cursor = None
    for _ in range(max_pages):
        params = {"status": "open", "series_ticker": series, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if "error" in data:
            break
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return markets

def fetch_global_pages(max_pages: int = GLOBAL_SWEEP_PAGES) -> list:
    """Pull first N pages of all open markets (no series filter)."""
    markets = []
    cursor = None
    for page in range(1, max_pages + 1):
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if "error" in data or not data:
            break
        batch = data.get("markets", [])
        markets.extend(batch)
        print(f"  Global page {page}: +{len(batch)} (total {len(markets)})")
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.1)
    return markets

# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _flt(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def days_out(close_time_str):
    if not close_time_str:
        return None
    try:
        exp = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
        return max(0, (exp - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return None

def parse_market(m, category):
    ticker = m.get("ticker", "")
    title = (m.get("title", "") + " " + m.get("subtitle", m.get("yes_sub_title", ""))).strip()
    series = m.get("series_ticker", "")

    yb = _flt(m.get("yes_bid_dollars"))
    ya = _flt(m.get("yes_ask_dollars"))
    lp = _flt(m.get("last_price_dollars"))
    yes_price = yb or lp or ya or 0.0

    vol24 = _flt(m.get("volume_24h_fp", m.get("volume_24h"))) or 0.0
    oi    = _flt(m.get("open_interest_fp", m.get("open_interest"))) or 0.0
    close = m.get("close_time", m.get("expected_expiration_time", ""))
    days  = days_out(close)

    return {
        "ticker": ticker,
        "series": series,
        "title": title[:80],
        "category": category,
        "yes_price": yes_price,
        "volume_24h": vol24,
        "open_interest": oi,
        "days": days,
    }

# ---------------------------------------------------------------------------
# Filter + Score
# ---------------------------------------------------------------------------

def passes(c):
    if c["volume_24h"] < 500:
        return False
    if c["open_interest"] < 100:
        return False
    yp = c["yes_price"]
    if yp < 0.03 or yp > 0.97:
        return False
    days = c["days"]
    if days is None or days > 90:
        return False
    return True

def score(c):
    vol = c["volume_24h"]
    oi  = c["open_interest"]
    yp  = c["yes_price"]
    if vol <= 0 or oi <= 0 or yp <= 0:
        return 0.0
    return math.log(vol) * math.log(oi) * (1 - abs(yp - 0.50))

# ---------------------------------------------------------------------------
# Agent + Priority tables
# ---------------------------------------------------------------------------

AGENT_MAP = {
    "Commodities": {
        "existing": None,
        "archetype": "**CommodityOracle** — data: EIA weekly inventory (free), USDA WASDE, commodity spot APIs. Edge: inventory surprise vs consensus. Oil/gold pricing is OBVIOUS; ag is SPECULATIVE (weather-driven).",
        "edge": "OBVIOUS (WTI/gold) · SPECULATIVE (ag)",
    },
    "Economics": {
        "existing": "Partial — `signals/fred.py` feeds FRED data; War Room `signals/macro_llm.py` uses LLM summary",
        "archetype": "**MacroSentinel** — data: FRED (free), BLS/BEA releases, Bloomberg consensus scrape. Edge: forecast consensus drift 48h pre-release vs Kalshi implied probability. Pattern: OBVIOUS.",
        "edge": "OBVIOUS",
    },
    "Companies": {
        "existing": None,
        "archetype": "**EarningsHunter** — data: Yahoo Finance consensus EPS, whisper numbers, options IV. Edge: IV-implied move vs Kalshi range spread. Pattern: SPECULATIVE without premium consensus data.",
        "edge": "SPECULATIVE",
    },
    "Financials": {
        "existing": "Partial — `signals/coinglass.py`, crypto signals in War Room",
        "archetype": "**MarketSentinel** — data: S&P/Nasdaq futures (free via yfinance), VIX term structure. Edge: end-of-day range contracts vs futures-implied distribution. Pattern: OBVIOUS for index ranges.",
        "edge": "OBVIOUS (index ranges) · SPECULATIVE (yield curve)",
    },
    "Tech": {
        "existing": "**contract_scanner.py** + **contract_matcher.py** (AU crypto side); War Room `signals/crypto.py`",
        "archetype": "**TechCatalyst** — data: AI benchmark leaderboards (LMSYS, HF), hyperscaler earnings calendar. Edge: binary event timing vs analyst consensus. Pattern: SPECULATIVE (launch dates uncertain).",
        "edge": "SPECULATIVE",
    },
    "Science": {
        "existing": None,
        "archetype": "**ScienceOracle** — data: NHC hurricane track (free + authoritative), FDA PDUFA calendar, NASA manifest. Edge: NHC 5-day cone vs Kalshi hurricane category pricing. Pattern: OBVIOUS (hurricane/weather in season), SPECULATIVE (FDA/space).",
        "edge": "OBVIOUS (hurricane season) · SPECULATIVE (FDA/space)",
    },
}

PRIORITY = [
    ("Economics",    "Highest liquidity (KXFED/KXCPI trade millions), FRED data free, consensus drift measurable 48h pre-release. Clear edge. Syndicate already has FRED signal pipeline."),
    ("Financials",   "S&P/Nasdaq range contracts liquid; futures provide authoritative intraday reference. Same-day daily range contracts are tractable. War Room coinglass signal can extend."),
    ("Commodities",  "WTI/gold contracts liquid; EIA data free. Natural complement to macro suite. Ag speculative but WTI alone justifies agent build."),
    ("Tech",         "AU already has crypto fleet + War Room crypto signals. Expanding to AI/product event contracts is natural extension. Non-crypto tech event volume is thin."),
    ("Companies",    "Earnings contracts exist and trade; needs premium consensus data (FactSet/Refinitiv) to reliably model EPS surprise. Higher setup cost."),
    ("Science",      "Hurricane contracts highly liquid IN-SEASON (Jun–Nov). FDA/space speculative. Seasonal strategy only — not year-round."),
]

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def fv(v):
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1000:.0f}K"
    return f"${v:.0f}"

def fp(p): return f"${p:.2f}" if p else "N/A"
def fd(d): return f"{d:.1f}d" if d is not None else "?"

def write_report(categorized: dict, output_path: str):
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Kalshi Market Survey — {now_str}",
        "",
        "**Scope:** Commodities · Economics · Companies · Financials · Tech · Science  ",
        "**Filters applied:** 24h vol ≥ $500 · OI ≥ 100 contracts · YES 3¢–97¢ · ≤ 90 days to settle  ",
        "**Tradability score:** `log(vol_24h) × log(OI) × (1 − |yes_price − 0.50|)`  ",
        "",
        "---",
        "## Section 1 — Category Summary",
        "",
        "| Category | Active Contracts | Tradable (post-filter) | Total 24h Volume | Avg Days to Settle |",
        "|----------|-----------------|----------------------|-----------------|-------------------|",
    ]

    all_tradable = {}
    CATS = ["Commodities", "Economics", "Companies", "Financials", "Tech", "Science"]

    for cat in CATS:
        raw = categorized.get(cat, [])
        tradable = sorted([c for c in raw if passes(c)], key=score, reverse=True)
        all_tradable[cat] = tradable
        total_vol = sum(c["volume_24h"] for c in tradable)
        avg_days = (sum(c["days"] for c in tradable if c["days"]) / len(tradable)) if tradable else 0
        lines.append(f"| {cat} | {len(raw)} | {len(tradable)} | {fv(total_vol)} | {avg_days:.1f}d |")

    lines += ["", "---", "## Section 2 — Top 10 Per Category", ""]

    for cat in CATS:
        tradable = all_tradable[cat]
        lines.append(f"### {cat}")
        if not tradable:
            lines.append("_No tradable contracts survived filters._")
            lines.append("")
            continue
        top10 = tradable[:10]
        lines += [
            "| Ticker | Title | YES | Vol 24h | OI | Days | Score |",
            "|--------|-------|-----|---------|----|----- |-------|",
        ]
        for c in top10:
            s = score(c)
            lines.append(f"| `{c['ticker']}` | {c['title'][:52]} | {fp(c['yes_price'])} | {fv(c['volume_24h'])} | {int(c['open_interest'])} | {fd(c['days'])} | {s:.2f} |")
        lines.append("")

    lines += ["---", "## Section 3 — Agent Mapping Recommendations", ""]

    for cat in CATS:
        info = AGENT_MAP[cat]
        lines.append(f"### {cat}")
        lines.append(f"**Existing AU/Syndicate coverage:** {info['existing'] or 'None'}")
        lines.append(f"**Recommended archetype:** {info['archetype']}")
        lines.append(f"**Edge pattern:** {info['edge']}")
        lines.append("")

    lines += ["---", "## Section 4 — Execution Priority", ""]
    lines += [
        "Ranked by: liquidity depth · edge availability · data availability · infrastructure alignment.",
        "",
        "| Rank | Category | Rationale |",
        "|------|----------|-----------|",
    ]
    for i, (cat, rationale) in enumerate(PRIORITY, 1):
        lines.append(f"| {i} | **{cat}** | {rationale} |")

    lines += ["", "---", "## BTC/ETH Fleet Comparison", ""]

    btc_eth_vol = sum(c["volume_24h"] for cats in all_tradable.values() for c in cats
                      if "BTC" in c["ticker"] or "ETH" in c["ticker"])
    econ_vol = sum(c["volume_24h"] for c in all_tradable.get("Economics", []))
    fin_vol  = sum(c["volume_24h"] for c in all_tradable.get("Financials", []))

    lines.append(f"- BTC/ETH-adjacent 24h volume in scan: **{fv(btc_eth_vol)}**")
    lines.append(f"- Economics tradable 24h volume: **{fv(econ_vol)}**")
    lines.append(f"- Financials tradable 24h volume: **{fv(fin_vol)}**")
    lines.append("")

    if econ_vol > btc_eth_vol or fin_vol > btc_eth_vol:
        lines.append("**Verdict:** Economics and/or Financials exceed BTC/ETH aggregate liquidity. Expansion clearly justified.")
    else:
        lines.append("**Verdict:** BTC/ETH fleet remains the deepest single category. Economics/Financials expansion is complementary — adds coverage without cannibalizing existing edge.")

    lines.append("")
    lines.append(f"_Generated by kalshi_market_survey.py · {now_str}_")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"\n[DONE] Report → {output_path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("[SURVEY] Starting Kalshi market survey (read-only)")
    seen_tickers = set()
    categorized = {cat: [] for cat in ["Commodities", "Economics", "Companies", "Financials", "Tech", "Science"]}

    # 1. Probe known series per category
    for cat, series_list in CATEGORY_SERIES.items():
        print(f"\n[PROBE] {cat} — {len(series_list)} series")
        for series in series_list:
            batch = fetch_series(series, max_pages=3)
            added = 0
            for m in batch:
                ticker = m.get("ticker", "")
                if not ticker or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                parsed = parse_market(m, cat)
                categorized[cat].append(parsed)
                added += 1
            if added:
                print(f"  {series}: +{added}")
            time.sleep(0.05)

    # 2. Global sweep — catch anything not in curated series list
    print(f"\n[SWEEP] Global market pages (first {GLOBAL_SWEEP_PAGES} pages)...")
    global_markets = fetch_global_pages(GLOBAL_SWEEP_PAGES)
    sweep_added = 0
    for m in global_markets:
        ticker = m.get("ticker", "")
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        title = (m.get("title", "") + " " + m.get("subtitle", "")).strip()
        cat = classify_by_title(title)
        if cat:
            parsed = parse_market(m, cat)
            categorized[cat].append(parsed)
            sweep_added += 1

    print(f"\n[SUMMARY] After probes + sweep:")
    total_tradable = 0
    for cat in ["Commodities", "Economics", "Companies", "Financials", "Tech", "Science"]:
        tradable = [c for c in categorized[cat] if passes(c)]
        total_tradable += len(tradable)
        print(f"  {cat}: {len(categorized[cat])} total, {len(tradable)} tradable")
    print(f"  Global sweep added: {sweep_added} new categorized markets")
    print(f"  Total unique tickers seen: {len(seen_tickers)}")
    print(f"  Total tradable across all categories: {total_tradable}")

    output_path = os.path.join(SYNDICATE_ROOT, "audits", "2026-04-20_kalshi_market_survey.md")
    write_report(categorized, output_path)
    return output_path

if __name__ == "__main__":
    main()
