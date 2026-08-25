"""Scan loop orchestrator.

Per cycle:
  1. Refresh signals snapshot
  2. Pull market list (live or mock)
  3. Filter via allowlist
  4. Score each market
  5. Identify opportunities >= threshold
  6. Size each opportunity
  7. Open positions
  8. Monitor existing positions (exits)
  9. Update last_scan timestamp + counters
"""
import time
from app import db
from app.config import MIN_VOLUME, TRADING_MODE
from signals import aggregate
from engine import kalshi, scoring, sizing, executor, allowlist


def _fetch_markets() -> list:
    """Get all open markets (paginated in LIVE, mock in PAPER without creds)."""
    if not kalshi.has_creds():
        return kalshi.mock_markets()

    out = []
    cursor = None
    while True:
        page = kalshi.list_markets(status="open", limit=200, cursor=cursor)
        if not page:
            break
        out.extend(page.get("markets", []))
        cursor = page.get("cursor")
        if not cursor or len(out) > 6000:  # safety bound
            break
    return out


def run_scan():
    started = time.time()
    db.state_set("last_scan_ts", int(started))
    db.state_set("scan_count", db.state_get("scan_count", 0) + 1)

    # 1. Signals
    signals = aggregate.snapshot()
    db.state_set("signals_snapshot", signals)

    # 2. Markets
    markets = _fetch_markets()
    db.state_set("market_count", len(markets))

    # 3+4+5. Filter, score, rank
    opportunities = []
    market_lookup = {}
    for m in markets:
        ticker = m["ticker"]
        market_lookup[ticker] = m

        if not allowlist.is_tradeable(ticker):
            continue
        if (m.get("volume_24h", 0) or 0) < MIN_VOLUME:
            continue

        ac = allowlist.asset_class_for(ticker)
        result = scoring.score_market(m, ac, signals)
        if result["passes_threshold"]:
            opportunities.append({
                "ticker": ticker,
                "asset_class": ac,
                "score": result["total"],
                "breakdown": result["breakdown"],
                "yes_ask": m.get("yes_ask"),
                "yes_bid": m.get("yes_bid"),
                "volume": m.get("volume_24h"),
                "hours_to_expiry": m.get("hours_to_expiry"),
                "close_time": m.get("close_time"),
            })

    opportunities.sort(key=lambda o: -o["score"])
    db.state_set("opportunities", opportunities[:20])

    # 8. Monitor existing positions FIRST (free up capital)
    executor.monitor_positions(market_lookup)

    # 6+7. Size + open new positions
    pv = executor.portfolio_value()
    cur_exp = executor.total_exposure()
    cur_sports = executor.sports_exposure()

    opened = 0
    for opp in opportunities[:5]:  # max 5 new entries per scan
        if opp["yes_ask"] is None:
            continue
        s = sizing.size_position(
            score=opp["score"],
            cost_cents=opp["yes_ask"],
            portfolio_value=pv,
            asset_class=opp["asset_class"],
            current_exposure=cur_exp,
            current_sports_exposure=cur_sports,
        )
        if s["contracts"] > 0:
            ok = executor.open_position(
                ticker=opp["ticker"],
                contracts=s["contracts"],
                ask_cents=opp["yes_ask"],
                asset_class=opp["asset_class"],
                expires_at=opp.get("close_time"),
            )
            if ok:
                opened += 1
                cur_exp += s["dollars"]
                if opp["asset_class"] == "sports":
                    cur_sports += s["dollars"]

    elapsed = round(time.time() - started, 2)
    db.log("DIGEST",
           f"Scan #{db.state_get('scan_count', 0)} — {len(markets)} markets / "
           f"{len(opportunities)} opps / {opened} opened",
           f"mode={TRADING_MODE} | elapsed={elapsed}s | pv=${pv:.2f}")


def boot():
    db.init_db()
    db.log("BOOT", "💩 NFE War Room — ONLINE",
           f"Mode: {TRADING_MODE} | Kalshi creds: "
           f"{'YES' if kalshi.has_creds() else 'NO (mock data)'}")
