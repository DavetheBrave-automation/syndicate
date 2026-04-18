# DELTA Fee Failure Audit — 2026-04-16

## Finding

After fee backfill across 101 historical syndicate_trades records, DELTA emerged as the
system's most significant fee-adjusted underperformer despite appearing near-breakeven
on gross P&L.

| Metric | Value |
|---|---|
| Trades | 31 |
| Gross P&L | +$0.02 |
| Total Fees Paid | -$7.48 |
| **Net P&L** | **-$7.50** |
| Gross Win Rate | 45.2% |
| **Net Win Rate** | **25.8%** |

## Root Cause Analysis

This is NOT a hold-time problem.

DELTA's HTSR (hold-to-settlement) fix improved exit timing, but exit timing was never
the root issue. The problem is entry edge. DELTA fires on arbitrage gaps that appear to
be ≥8% but are being filled by the market within the trade window faster than the fees
can be recovered.

Possible explanations:
1. **Gap stale by the time DELTA sees it** — BTC arb signals reference Coinbase spot;
   by the time TC processes and fills, the gap has narrowed below fee breakeven.
2. **BTC ladder spam** — DELTA evaluates multiple near-settlement KXBTCD strikes at once.
   The MAX_SIGNALS_PER_CYCLE=3 cap helps but the underlying "find the 8% gap" logic
   may be triggering on stale or mean-reverting spreads.
3. **Fee ignorance in edge calculation** — DELTA computes edge_pct from the gross
   arb gap, not the net gap after fees. At $3-4 stakes (old sizing), fee drag was ~14%
   of stake per round-trip. A claimed "10% edge" was actually -4% net.

## Decision

**DELTA stays on PROBATION indefinitely pending entry logic review.**

Standard tier promotion math says:
- 20 trades + 50% WR → QUALIFIED ($250 max)
- DELTA has 31 trades, gross 45.2% WR → would qualify by gross math alone

**OVERRIDE: Do not promote DELTA to QUALIFIED tier regardless of gross sample size.**

Promotion criteria for DELTA (special case):
- Minimum 40 trades at correct sizing ($25 PROBATION stake)
- Net win rate ≥ 45% over trailing 30 trades
- Net P&L positive over trailing 20 trades

Add explicit block to syndicate_config.yaml or tier_manager when DELTA approaches threshold.

## Action Items

- [x] Comment block added to agents/delta.py evaluate() — 2026-04-16
- [x] This audit doc created — 2026-04-16
- [ ] Review DELTA's entry edge logic — specifically whether BTC ladder gap is measured
      at signal time vs fill time
- [ ] Consider adding fee_drag_pct to all agent signal outputs so TC can see net edge
      before approving trades
- [ ] Monday review: check DELTA's next 10 trades at $25 stake — are they trending up?

## Timeline

- 2026-04-16: Fee backfill reveals -$7.50 net on 31 trades
- 2026-04-16: DELTA benched from QUALIFIED promotion; PROBATION cap maintained
- Next review: 2026-04-21 (after 10 more trades at correct $25 sizing)
