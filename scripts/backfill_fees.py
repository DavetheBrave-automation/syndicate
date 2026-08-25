"""
scripts/backfill_fees.py — One-time migration: backfill fees_paid and net_pnl
for all historical syndicate_trades records.

Run once from the syndicate root:
    python scripts/backfill_fees.py

Safe to re-run: only updates rows where fees_paid IS NULL.

Fee formula: 0.07 × quantity × min(yes_price, no_price) per leg (entry + exit).
Source: audits/2026-04-16_kalshi_fees.md

IMPORTANT FLAGS TO WATCH:
  - Any agent whose net P&L flips from positive to negative after fee backfill
  - Any agent whose net win rate drops below 50%
These are intelligence signals for the Friday go-live decision.
"""

import os
import sys
import sqlite3
from collections import defaultdict

_SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT = os.path.dirname(_SCRIPT_DIR)
_DB_PATH        = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")

sys.path.insert(0, _SYNDICATE_ROOT)

FEE_RATE = 0.07  # must match core/fee_calculator.py


def _fee(quantity: int, price_cents: int) -> float:
    """Fee for one leg: 0.07 × qty × min(yes_price, no_price)."""
    yes_price = price_cents / 100.0
    no_price  = 1.0 - yes_price
    return FEE_RATE * quantity * min(yes_price, no_price)


def run_backfill():
    if not os.path.exists(_DB_PATH):
        print(f"ERROR: DB not found at {_DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Ensure fee columns exist
    for col, typedef in [("fees_paid", "REAL"), ("net_pnl", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE syndicate_trades ADD COLUMN {col} {typedef}")
            conn.commit()
            print(f"[backfill] Added column: {col}")
        except Exception:
            pass  # already exists

    # Fetch all rows that need backfill
    rows = conn.execute("""
        SELECT id, entry_price, exit_price, quantity, pnl, agent_name
        FROM syndicate_trades
        WHERE exit_time IS NOT NULL
          AND fees_paid IS NULL
        ORDER BY id ASC
    """).fetchall()

    if not rows:
        print("[backfill] No rows to backfill — all trades already have fee data.")
        conn.close()
        _print_summary(sqlite3.connect(_DB_PATH))
        return

    print(f"[backfill] Found {len(rows)} trades to backfill...")

    updated = 0
    for row in rows:
        entry_cents  = int(row["entry_price"] or 0)
        exit_price   = float(row["exit_price"] or 0.0)
        exit_cents   = int(round(exit_price * 100))
        qty          = int(row["quantity"] or 0)
        gross_pnl    = float(row["pnl"] or 0.0)

        if qty <= 0 or (entry_cents == 0 and exit_cents == 0):
            fees_paid = 0.0
        else:
            fees_paid = round(_fee(qty, entry_cents) + _fee(qty, exit_cents), 4)

        net_pnl = round(gross_pnl - fees_paid, 4)

        conn.execute(
            "UPDATE syndicate_trades SET fees_paid=?, net_pnl=? WHERE id=?",
            (fees_paid, net_pnl, row["id"]),
        )
        updated += 1

    conn.commit()
    print(f"[backfill] ✓ Backfilled {updated} trades.\n")

    _print_summary(conn)
    conn.close()


def _print_summary(conn):
    """Print per-agent impact report: gross vs net P&L, flag flips."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT agent_name,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as gross_wins,
               SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END) as net_wins,
               ROUND(SUM(pnl), 2) as gross_pnl,
               ROUND(SUM(COALESCE(fees_paid, 0)), 2) as total_fees,
               ROUND(SUM(COALESCE(net_pnl, pnl)), 2) as net_pnl
        FROM syndicate_trades
        WHERE exit_time IS NOT NULL
        GROUP BY agent_name
        ORDER BY net_pnl DESC
    """).fetchall()

    print("=" * 70)
    print(f"{'AGENT':<10} {'TRADES':>6} {'GROSS P&L':>10} {'FEES':>8} {'NET P&L':>10} {'GWIN%':>6} {'NWIN%':>6} {'FLAG'}")
    print("-" * 70)

    flipped_agents = []
    win_rate_warned = []

    for r in rows:
        agent      = (r["agent_name"] or "?")
        trades     = int(r["trades"] or 0)
        gross_wins = int(r["gross_wins"] or 0)
        net_wins   = int(r["net_wins"] or 0)
        gross_pnl  = float(r["gross_pnl"] or 0.0)
        total_fees = float(r["total_fees"] or 0.0)
        net_pnl    = float(r["net_pnl"] or 0.0)
        g_wr       = round(gross_wins / trades * 100, 1) if trades else 0.0
        n_wr       = round(net_wins   / trades * 100, 1) if trades else 0.0

        flag = ""
        if gross_pnl > 0 and net_pnl <= 0:
            flag = "⚠ FLIPPED NEGATIVE"
            flipped_agents.append(agent)
        elif n_wr < 50.0 and trades >= 5:
            flag = "⚠ WIN RATE <50%"
            win_rate_warned.append(agent)

        print(
            f"{agent:<10} {trades:>6} "
            f"{'%+.2f'%gross_pnl:>10} {'%+.2f'%(-total_fees):>8} {'%+.2f'%net_pnl:>10} "
            f"{g_wr:>5.1f}% {n_wr:>5.1f}% {flag}"
        )

    print("=" * 70)

    if flipped_agents:
        print(f"\n🚨 AGENTS FLIPPED NEGATIVE after fee backfill: {', '.join(flipped_agents)}")
        print("   These agents were not beating Kalshi fees at old $3-4 stake sizes.")
        print("   This is expected — and is exactly why correct sizing matters.")
    if win_rate_warned:
        print(f"\n⚠  AGENTS WITH NET WIN RATE <50%: {', '.join(win_rate_warned)}")
        print("   Consider whether these agents have enough trades to draw conclusions.")
    if not flipped_agents and not win_rate_warned:
        print("\n✅ No agents flipped negative. Fee drag absorbed within existing edge.")

    # Totals
    total_gross = sum(float(r["gross_pnl"] or 0) for r in rows)
    total_fees  = sum(float(r["total_fees"] or 0) for r in rows)
    total_net   = sum(float(r["net_pnl"] or 0) for r in rows)
    print(f"\nSYSTEM TOTALS:")
    print(f"  Gross P&L: ${total_gross:+.2f}")
    print(f"  Total Fees: -${total_fees:.2f}")
    print(f"  Net P&L:   ${total_net:+.2f}")
    print(f"  Fee drag:  {abs(total_fees / total_gross * 100):.1f}% of gross P&L" if total_gross != 0 else "")


if __name__ == "__main__":
    run_backfill()
