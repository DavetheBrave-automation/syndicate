"""
edge_diagnostic.py — One-time diagnostic: edge calculation vs 10% floor.

For every live NBA/MLB/NHL market passing liquidity, simulate what each
agent would compute for edge and log WHY it passes or fails MIN_EDGE_PCT.

Run once manually:
    python tools/edge_diagnostic.py

Output goes to stdout and logs/edge_diagnostic.log
"""

import os
import sys
import logging

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

# ---------------------------------------------------------------------------
# Logging — stdout + file
# ---------------------------------------------------------------------------

_LOG_PATH = os.path.join(_SYNDICATE_ROOT, "logs", "edge_diagnostic.log")
os.makedirs(os.path.join(_SYNDICATE_ROOT, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_PATH, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("edge_diag")

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from connectors.kalshi_rest import get_all_markets
from core.liquidity_filter import check_market
from core.shared_state import MarketData

MIN_EDGE_PCT = 7.0
TARGET_SERIES = {"KXNBA", "KXMLB", "KXNHL"}


# ---------------------------------------------------------------------------
# AXIOM edge formula (replicated from agents/axiom.py)
# ---------------------------------------------------------------------------

def _axiom_edge(m: MarketData):
    displacement = abs(m.yes_price - 0.5)
    days         = m.days_to_settlement
    time_factor  = max(0.1, 1.0 - days * 0.3)
    vol_factor   = min(1.0, m.volume_dollars / 10_000)   # sports-scale
    edge_pct     = displacement * time_factor * vol_factor * 100.0
    return edge_pct, displacement, days, time_factor, vol_factor


# ---------------------------------------------------------------------------
# DELTA edge estimate (replicated from agents/delta.py)
# ---------------------------------------------------------------------------

_DELTA_MIN_EDGE = 8.0
_PROPHECY_EDGE  = 20.0

def _delta_edge(m: MarketData):
    # DELTA always uses a fixed estimate — actual edge computed by TC after web search
    edge_pct_est = _DELTA_MIN_EDGE + 2.0  # 10%
    return edge_pct_est


# ---------------------------------------------------------------------------
# BLITZ edge (velocity-based — no static formula, needs price history)
# ---------------------------------------------------------------------------

def _blitz_note():
    return "velocity-based — requires live price history (no static formula)"


# ---------------------------------------------------------------------------
# SAGE edge (spread-based momentum — simplified)
# ---------------------------------------------------------------------------

def _sage_edge(m: MarketData):
    # SAGE looks for spread compression — proxy: spread vs volume
    spread_pct = m.spread / max(m.yes_price, 0.01) * 100
    return spread_pct


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

def run():
    log.info("=" * 70)
    log.info("EDGE DIAGNOSTIC — NBA/MLB/NHL markets vs 7%% floor (vol_factor sports-scale)")
    log.info("=" * 70)

    log.info("Fetching live markets...")
    raw = get_all_markets()
    log.info("Fetched %d total markets.", len(raw))

    # Build MarketData objects, filter to target series
    target_markets = []
    for r in raw:
        series = r.get("series_ticker", r.get("ticker", "").split("-")[0])
        if series not in TARGET_SERIES:
            continue
        m = MarketData(
            ticker             = r["ticker"],
            yes_price          = float(r.get("yes_price", 0.5)),
            no_bid             = float(r.get("no_bid", 0.5)),
            volume_dollars     = float(r.get("volume_dollars", 0)),
            spread             = float(r.get("spread", 0.0)),
            days_to_settlement = float(r.get("days_to_settlement", 1)),
            contract_class     = "SWING",   # placeholder — classify_market overwrites
            series_ticker      = series,
            last_update        = 0.0,
        )
        target_markets.append(m)

    log.info("Target series markets found: %d", len(target_markets))
    log.info("")

    # Run liquidity filter
    liquid = []
    rejected = []
    for m in target_markets:
        result = check_market(m.ticker, m)
        if result.passed:
            liquid.append((m, result))
        else:
            rejected.append((m, result))

    log.info("Liquidity filter: %d passed / %d rejected", len(liquid), len(rejected))
    log.info("")

    if not liquid:
        log.warning("NO markets passed liquidity — nothing to diagnose.")
        log.info("Rejected sample (first 5):")
        for m, r in rejected[:5]:
            log.info("  REJECTED %s | vol=$%,.0f spread=%.3f days=%.1f reason=%s",
                     m.ticker, m.volume_dollars, m.spread, m.days_to_settlement, r.rejection_reason)
        return

    # ── Per-market edge breakdown ────────────────────────────────────────────
    log.info("%-45s %-8s %-6s %-6s %-8s %-8s %-8s  RESULT",
             "TICKER", "SERIES", "PRICE", "DAYS", "DISP", "AXIOM%", "vs10%")
    log.info("-" * 110)

    axiom_pass = axiom_fail = 0
    fail_reasons = {}

    for m, liq in sorted(liquid, key=lambda x: x[0].series_ticker):
        edge, disp, days, tf, vf = _axiom_edge(m)
        passes = edge >= MIN_EDGE_PCT
        verdict = "PASS" if passes else f"FAIL (need {MIN_EDGE_PCT:.0f}%)"

        if passes:
            axiom_pass += 1
        else:
            axiom_fail += 1
            # Diagnose the limiting factor
            if vf < 0.5:
                reason = f"vol_factor={vf:.2f} (vol=${m.volume_dollars:,.0f} << $10k)"
            elif tf < 0.5:
                reason = f"time_factor={tf:.2f} (days={days:.1f} — too far out)"
            elif disp < 0.15:
                reason = f"displacement={disp:.2f} (yes_price={m.yes_price:.2f} — near 50¢)"
            else:
                reason = f"combined: disp={disp:.2f} tf={tf:.2f} vf={vf:.2f}"
            fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

        log.info("%-45s %-8s %-6.2f %-6.1f %-8.2f %-8.2f %-8.1f  %s",
                 m.ticker[:45], m.series_ticker, m.yes_price, days,
                 disp, edge, MIN_EDGE_PCT, verdict)

    log.info("")
    log.info("AXIOM SUMMARY: %d PASS / %d FAIL out of %d liquid markets",
             axiom_pass, axiom_fail, len(liquid))

    if fail_reasons:
        log.info("")
        log.info("FAIL ROOT CAUSES:")
        for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1]):
            log.info("  [%d markets] %s", count, reason)

    # ── Edge floor verdict ───────────────────────────────────────────────────
    log.info("")
    log.info("VERDICT:")
    if axiom_fail == 0:
        log.info("  10%% floor is fine — all liquid markets pass AXIOM edge.")
    elif axiom_fail > axiom_pass:
        log.info("  10%% floor is TOO CONSERVATIVE — majority fail.")
        log.info("  Consider: lower MIN_EDGE_PCT to 7%% (AXIOM's own internal threshold)")
        log.info("  or adjust time_factor / vol_factor scaling for sports contracts.")
    else:
        log.info("  10%% floor is borderline — %d/%d fail. Review fail reasons above.",
                 axiom_fail, len(liquid))

    log.info("")
    log.info("DELTA note: always estimates edge=10%% (floor minimum) — TC computes actual after web search.")
    log.info("BLITZ note: %s", _blitz_note())
    log.info("")
    log.info("Full output saved to: %s", _LOG_PATH)
    log.info("=" * 70)


if __name__ == "__main__":
    run()
