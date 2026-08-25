# Fee Optimization Analysis
**Status:** Read-only — no code changes  
**Date:** 2026-04-18  
**DB baseline:** 117 closed trades | -$69.69 net P&L | $87.01 total fees  
**Scope:** Tasks 1–5 per spec

---

## Task 1 — Kalshi Fee Structure Audit

### Documented Fee Schedule (source: Kalshi docs as of Aug 2025, confirmed in `audits/2026-04-16_kalshi_fees.md`)

| Parameter | Value | Confirmed in code? |
|-----------|-------|--------------------|
| Taker fee rate | 7% of `min(yes_price, no_price)` per contract | YES — `core/fee_calculator.py` FEE_RATE = 0.07 |
| Maker fee rate | **0%** — limit orders that rest on the book | NOT IMPLEMENTED (code charges 7% on all orders) |
| Fee timing | Charged at fill, per contract | YES — entry + exit computed separately |
| YES vs NO symmetry | Same fee — based on cheaper side | YES — `min(yes_price, no_price)` formula |
| Volume tier discounts | Market maker tier only, not retail | N/A for Syndicate |
| Fee basis | Cheaper side of contract (the actual cost) | YES — formula is correct |

### Fee Formula (current implementation — correct)

```
fee_per_leg = 0.07 × quantity × min(yes_price, 1 - yes_price)
round_trip_fee = entry_fee + exit_fee
```

### Discrepancy Identified: One Major Gap

**The code correctly calculates fees but applies the same rate (7%) to ALL order fills — market AND limit.**

In reality:
- Market order fill = **taker** = 7% fee
- Limit order resting then filled = **maker** = **0% fee**

The current `fee_calculator.py` has no `order_type` parameter. It always charges 7%. This is correct for paper mode (which assumes market fills) but will be incorrect in live mode if limit entries are used. `order_manager.py` live mode already calls `place_limit_buy(price + 0.03)` — an aggressive limit that crosses the spread and fills as a taker. This is fine for now. The gap opens when resting (passive) limits are introduced.

### Verification Required Before Live Mode

The 7% taker / 0% maker structure should be verified against:
1. Kalshi's current API fee documentation (https://kalshi.com/fees or developer docs)
2. The first live fill confirmation in account history
3. Account tier — confirm Syndicate's account is on "Advanced" retail tier, not a separate maker/taker structure

**Confidence: MEDIUM-HIGH.** The formula has been validated against actual trade data (fee_backfill_report, Apr 16) and all 117 DB rows reconcile to within rounding tolerance.

---

## Task 2 — Retroactive Fee Savings Analysis

### Methodology

- **Scenario A (actual):** taker entry + taker exit = `fees_paid` as recorded
- **Scenario B (maker entry + market exit):** 0% entry fee + 7% exit fee = `exit_fee` only
- **Scenario C (maker/maker):** 0% entry + 0% exit = 0 fees = gross `pnl`

Entry fee = `0.07 × qty × min(entry_price/100, 1 - entry_price/100)`
Exit fee  = `0.07 × qty × min(exit_price, 1 - exit_price)`

DB encoding note: `entry_price` stored in cents (integer), `exit_price` stored as decimal. Both verified.

### Per-Agent Fee Scenarios

| Agent | Trades | Gross P&L | Net (Mkt/Mkt) | Maker-Entry Net | Maker/Maker Net | Entry Fee Saved | Full Fee Saved |
|-------|--------|-----------|---------------|-----------------|-----------------|-----------------|----------------|
| AXIOM | 33 | +$46.12 | **+$25.70** | **+$36.99** | **+$46.12** | $11.22 | $20.41 |
| SHADOW | 2 | +$4.48 | +$4.23 | +$4.36 | +$4.48 | $0.12 | $0.25 |
| GHOST | 3 | -$0.38 | -$0.80 | -$0.58 | **-$0.38** | $0.22 | $0.42 |
| ACE | 6 | -$1.33 | -$3.32 | -$2.24 | -$1.33 | $1.08 | $1.99 |
| PHOENIX | 3 | -$4.41 | -$6.04 | -$5.07 | -$4.41 | $0.97 | $1.63 |
| DIAMOND | 3 | -$3.63 | -$11.09 | **-$7.22** | **-$3.63** | $3.85 | $7.46 |
| DELTA | 39 | -$8.65 | -$30.51 | **-$19.16** | **-$8.65** | $11.34 | $21.86 |
| CIPHER | 28 | -$14.87 | -$47.87 | -$30.10 | -$14.87 | $17.74 | $33.00 |
| **SYSTEM** | **117** | **+$17.32** | **-$69.69** | **-$23.02** | **+$17.32** | **$46.54** | **$87.01** |

### Win Rate Shift by Scenario

| Agent | WR (Mkt/Mkt) | WR (Maker Entry) | WR (Maker/Maker) | Delta |
|-------|-------------|-----------------|-----------------|-------|
| AXIOM | 51.5% | 51.5% | 54.5% | +3.0pp |
| DELTA | 25.6% | 30.8% | 43.6% | +18.0pp |
| CIPHER | 64.3% | 64.3% | 71.4% | +7.1pp |
| DIAMOND | 33.3% | 33.3% | 33.3% | 0pp |

### Edge Resurrection Analysis

**Question:** Does any currently-negative agent flip net-positive under maker pricing?

| Agent | Maker-Entry Net | Maker/Maker Net | Salvageable? |
|-------|----------------|-----------------|--------------|
| GHOST | -$0.58 | **-$0.38** | Near breakeven under maker/maker — tiny sample (n=3) |
| ACE | -$2.24 | -$1.33 | Still negative; problem is 16.7% WR, not fees |
| PHOENIX | -$5.07 | -$4.41 | Still negative; 0% WR is the problem |
| DIAMOND | -$7.22 | **-$3.63** | Dramatically better — from -$11.09 to -$3.63. At $2.48/trade fee drag, DIAMOND is fee-crushed. |
| DELTA | -$19.16 | **-$8.65** | Still negative gross. DELTA loses money even before fees. Fee optimization is necessary but not sufficient. |
| CIPHER | -$30.10 | **-$14.87** | The gross P&L is -$14.87. CIPHER has a structural win/loss asymmetry (see below). |

**No agent flips from negative to positive under maker-entry alone.**  
**No agent flips from negative to positive under maker/maker pricing.**

**However, the SYSTEM flips from -$69.69 to +$17.32 under maker/maker** — because AXIOM's gross P&L is +$46.12 and the other losses shrink dramatically. The aggregate portfolio breaks even only if AXIOM is retained and other agents' losses are contained.

### CIPHER Structural Diagnosis

CIPHER's 64.3% win rate masking gross losses is the most important finding:

| Metric | Value |
|--------|-------|
| Gross win rate | 71.4% (before fees) |
| Net win rate | 64.3% (after fees) |
| Avg win | +$1.47 |
| Avg loss | -$7.43 |
| Win/loss ratio | **0.20x** |
| Max single win | +$13.79 |
| Max single loss | -$48.38 (pre-cap, trade #108) |

**Finding:** CIPHER wins frequently but each win is small; losses are catastrophic. At 64% win rate with 0.20x win/loss ratio, the system needs win/loss ≥ 0.56x to break even. Fee optimization is irrelevant until the loss asymmetry is addressed. The hard cap (10 contracts, applied Apr 18) limits future max losses; whether it's sufficient requires 30+ post-cap trades.

### DELTA Diagnosis

DELTA's gross P&L is -$8.65 before any fees — the fees ($21.86) amplify a losing gross position into a -$30.51 disaster. Maker pricing would save $21.86 in fees, reducing to -$8.65, but the underlying edge is still negative. Fix B (240-min max hold) may help but doesn't address the core issue.

### DIAMOND Diagnosis

DIAMOND's fee-to-gross ratio is extreme: 3 trades, -$3.63 gross, $7.46 fees = net -$11.09. DIAMOND's positions are large ($100 stake on HIGH_CONVICTION) but the gross P&L is near-zero. Fee drag of $2.48/trade is the dominant force on a $100 stake — that's 2.5% per-trade drag, requiring a consistent edge > 5% just to cover fees. Under maker/maker, DIAMOND is only -$3.63 — **maker entry is near-essential for DIAMOND to ever be viable**.

---

## Task 3 — Fill Rate Simulation

### Methodology and Limitations

**What we have:** Entry price, exit price, hold duration per trade.  
**What we lack:** Intra-trade order book depth, bid-ask spread at entry time, price path between entry and exit.

**Fill rate estimation:** Based on price movement range (`abs(exit_price - entry_price)` in cents) as a proxy for market activity during the hold. A limit order 1¢ better than market will fill if:
- The market is liquid AND the spread allows resting (maker)
- OR price moves ≥ 1¢ toward our limit level during the hold window

**Known gap:** This measures range during the entire hold, not at the entry moment. A trade that immediately moved against us may still have covered ≥ 2¢ range — but the limit might not have filled before the move.

### Results by Contract Type

| Contract Type | Trades | Price Range ≥ 2¢ | Price Range ≥ 5¢ | Avg Hold | Fill Rate Est. (1¢ better) |
|---------------|--------|-----------------|-----------------|----------|---------------------------|
| BTC_LADDER | 91 | **75%** | 65% | 26.7 min | ~60–70% (conservative) |
| ETH_LADDER | 2 | 100% | 50% | 14.6 min | ~80% |
| PGA_GOLF | 12 | **92%** | **92%** | 16.0 min | ~80–90% |
| TENNIS | 12 | **92%** | 75% | 9.5 min | ~70–80% |

**Interpretation of BTC 75% figure:**

75% of BTC trades moved ≥ 2¢ during the hold. For a limit order 1¢ better than market:
- Winning trades (price moved our direction): limit may NOT fill (price ran away immediately)
- Losing trades (price moved against us): limit likely fills (price came toward us before reversing)
- Mixed/choppy trades: fill probability ~50-70%

Conservative blended estimate: **60-70% maker fill rate on BTC ladder entries** at 1¢ better than market. PGA and sports are higher (~80-90%) because these markets move slower and orders rest longer.

### Price Bucket Analysis for BTC Ladder

Entry price distribution across 91 BTC trades:

| Price Bucket | Trades | Typical Spread Est. | Limit Viability |
|--------------|--------|---------------------|-----------------|
| 01–10¢ | ~15 | 1–3¢ | **POOR** — tiny spread, limit gets stuck at illiquid levels |
| 11–30¢ | ~25 | 2–5¢ | MODERATE — spread allows some maker resting |
| 31–50¢ | ~30 | 2–4¢ | **GOOD** — mid-range contracts are most liquid |
| 51–70¢ | ~15 | 3–6¢ | MODERATE — equivalent to 30–49¢ by symmetry |
| 71–90¢ | ~6 | 1–3¢ | **POOR** — same as <10¢ by symmetry |

**Implication:** Limit orders are most effective for contracts priced 30–70¢ YES. Near extremes (< 10¢ or > 90¢), the market is thin and a limit order may sit unfilled.

### Hypothetical Limit Fill Rates Summary

| Order Strategy | BTC Fill Rate | PGA Fill Rate | System Fill Rate |
|---------------|---------------|---------------|-----------------|
| Market (current) | 100% | 100% | 100% |
| Limit @ market ask | ~95% | ~90% | ~94% (some non-fill risk in illiquid moments) |
| Limit @ 1¢ better | ~60-70% | ~80-90% | ~65-75% |
| Limit @ 2¢ better | ~40-55% | ~65-80% | ~50-60% |

**The fill rate drop is the core trade-off:** saving 7% in entry fees vs risking 25-35% signal miss rate.

---

## Task 4 — Order Strategy Recommendations

### 4a. Default Order Type Per Agent

| Agent | Recommended Entry | Rationale |
|-------|------------------|-----------|
| **AXIOM** | Maker entry (limit at bid) | PGA and BTC markets — high fill rate for resting limits. Entry fee savings: $11.22 over 33 trades = $0.34/trade on a ~$15 average stake. |
| **CIPHER** | Maker entry (limit at bid or mid) | Entry fee savings: $17.74 over 28 trades = $0.63/trade. At hard cap of 10 contracts, entry fee per trade is small (~$0.10–0.20) — maker entry nearly eliminates it. |
| **DELTA** | Maker entry with timeout fallback | Long holds (41 min avg) — missing entry by 30–60s costs less than the fee. Entry fee savings: $11.34, but signal staleness risk is real on BTC ladder (prices move fast). Recommend: limit for 30s, then market fallback. |
| **DIAMOND** | **Maker entry critical** | At $2.48/trade fee drag, maker entry saves $3.85 of the $7.46 total fee burden. DIAMOND's position size requires maker pricing to be viable at all. |
| **GHOST/ACE/PHOENIX** | Market entry | Small stakes, infrequent signals, tennis/ATP markets are thin. Market entry ensures fill. Fee savings tiny vs risk of missing the entry entirely. |
| **BLITZ** | Market entry ONLY | Velocity-dependent — signal is time-critical. A limit order that rests defeats the purpose. Taker fee is the cost of speed. |
| **TIDE** | Market entry | Momentum-following — limit entry would miss the entry on strong moves. |
| **MIRROR** | Maker entry | Post-stabilization setup — price is moving slowly by definition. Limit order at entry ± 1¢ is safe. |
| **ORACLE** | Maker entry | Long-hold political contracts — price is stable, fill rate would be ~95%. |

### 4b. Stop-Loss Strategy Under Limit Exits

Three approaches:

**Option 1 — Market exit for stops (current approach)**  
- Guarantees exit. Fee: 7% of min(yes_price, no_price).  
- On a losing trade exiting at 80¢ YES (20¢ NO), exit fee = 0.07 × qty × 0.20 = 1.4% of stake.  
- Acceptable: you're already losing on the trade, paying fee to exit is correct.

**Option 2 — Limit exit for stops**  
- Risk of non-fill. A stop at 30¢ with a limit order might not fill if the market gaps through 30¢.  
- Not recommended for binary event risk (BTC news, match conclusion).

**Option 3 — Hybrid: limit exit first, market fallback**  
- Post limit order at stop price. If not filled within N seconds (30–60s), cancel and market sell.  
- Saves fee on orderly retreats; guarantees exit on gap moves.  
- **Recommended for DELTA and DIAMOND** (longer holds, less urgent stops).

**Target exits** (winning trades): Limit exit at target price. Since we're profiting, we can afford to rest. This is already how target exits work in practice — TC places the exit when price hits the target, and a limit order closer to mid would save the exit fee.

**Summary recommendation:**

| Leg | Strategy | Rationale |
|-----|----------|-----------|
| Entry — calm/trending | Maker limit + 30s market fallback | Save 7% entry fee; 30s timeout prevents staleness |
| Entry — velocity/momentum (BLITZ) | Market only | Time-critical, fee is cost of speed |
| Stop-loss exit | Market (always) | Guarantee fill; accept fee to limit further loss |
| Target exit | Maker limit (no fallback needed) | We own the position; limit at target is clean |

### 4c. Implementation Complexity in `order_manager.py`

**Current state:**
- Paper mode: instant fill at entry_price — no order type
- Live mode: `place_limit_buy(price + 0.03)` — aggressive limit (crosses spread, taker)

**Changes needed for maker entry:**

1. Add `order_type` parameter to `place_order()`: `"market"` | `"aggressive_limit"` (current) | `"passive_limit"` (maker)
2. For `passive_limit`: call `place_limit_buy(price, side, qty, price)` — exact price, resting
3. Add `limit_timeout_seconds` parameter: if 0, pure maker; if > 0, cancel and market-fill on timeout
4. Track pending limit orders: need a `_pending_limits` dict (ticker → order_id)
5. Background thread or heartbeat check: `check_pending_limit_fills()` every 5–10s

**Kalshi fill notification — polling vs webhooks:**  
Kalshi's REST API supports order status polling: `GET /portfolio/orders/{order_id}`.  
WebSocket events: the existing `kalshi_ws.py` may receive fill events on the user channel (needs verification — check if the WS subscription includes `order_filled` events).  
**If WS fill events are available:** integrate fill check into the existing WS message handler.  
**If not:** poll `GET /portfolio/orders/{order_id}` every 5s in a background thread.  
**Action required:** Verify Kalshi WS spec for order fill events before implementing.

**Partial fills:**  
Kalshi binary options can partially fill on limit orders (e.g., you post 33 contracts, only 20 fill). `order_manager.py` would need to:
- Track filled quantity vs requested quantity
- Either (a) proceed with partial fill and adjust position size, or (b) wait for full fill within timeout

**Paper mode accuracy:**  
Current paper mode simulates instant fill at the exact entry price. To simulate limit order behavior accurately:
- Model fill probability based on price bucket and contract type (Task 3 data)
- Introduce random non-fill events: 25–35% of resting limits miss on BTC
- This would make paper P&L more realistic and expose the fill-rate risk in advance

**Estimated implementation scope:**  
- `order_manager.py`: ~150 lines new code (limit order state tracking, timeout, cancel)
- `kalshi_rest.py`: likely needs `cancel_order()` and `get_order_status()` — verify if implemented
- Paper mode changes: ~50 lines (fill probability simulation)

---

## Task 5 — Risk Assessment

### Risk 1: Signal Staleness While Waiting for Limit Fill

**Scenario:** Agent detects KXBTCD YES at 45¢ with 20% edge. Posts limit at 44¢ (1¢ below market). Market moves to 42¢ before limit fills. By fill time, the edge is different (smaller or reversed).

**Impact:** High for BTC ladder — BTC can move 5¢ in 30 seconds. The "signal" was valid at 45¢ but the fill at 44¢ is now entering a moving market.

**Mitigation:** 
- Limit timeout (30s for BTC, 60s for PGA/tennis)
- Cancel and reassess, NOT auto-market-fallback, on large moves
- Add stale signal check before market fallback: if `|current_price - original_signal_price| > threshold`, cancel and discard

**Risk level: HIGH for BLITZ/CIPHER (velocity-dependent), MODERATE for DELTA/AXIOM**

---

### Risk 2: Market Moving Away Before Limit Hits

**Scenario:** DELTA signals NO at 25¢. Posts limit at 24¢. Market immediately runs to 20¢ (BTC drops). DELTA misses the entry entirely.

**Impact:** On winning trades specifically — the best trades (large fast moves) are exactly the ones where a limit 1¢ better won't fill. The fill rate analysis suggests 25–40% of winning BTC trades would be missed.

**Quantified impact on AXIOM:** AXIOM win rate is 51.5%. Of winning trades, assume 50% would miss the limit entry at 1¢ better → ~25% of all AXIOM trades are missed winners. This partially negates the entry fee savings.

**Counter-argument:** If the trade would have been a winner anyway, we capture *more* profit per contract at the lower entry price — so the 65–75% of trades that do fill show better P&L per-fill than the market-order baseline.

**Net expected value (rough calculation):**
- Entry fee saved per fill: 7% of min(yes_price, no_price) ≈ $0.30–0.60 per trade
- Expected miss rate on winning trades: ~25% (offset by better entry price on fills)
- This math likely favors maker entry for longer-hold contracts (DELTA, DIAMOND) and is neutral-to-negative for short holds (CIPHER scalps)

**Risk level: MODERATE for DELTA/DIAMOND, HIGH for CIPHER/BLITZ**

---

### Risk 3: Partial Fills on Chunked Orders

**Scenario:** CIPHER posts 10 contracts at 60¢ YES. Only 7 fill. Engine enters a 7-contract position.

**Impact:**
- DB entry needs to record actual filled quantity, not requested quantity
- Position sizing calculation is off (exposure tracking)
- If partial fill is below min threshold (e.g., 3 contracts on a $6 stake), may not be worth holding

**Current handling:** Paper mode assumes full fill. Live mode (`place_limit_buy`) does not implement partial fill tracking.

**Mitigation:** Add minimum fill fraction threshold: if less than 50% of order fills within timeout, cancel remainder and either (a) proceed with partial, or (b) discard position.

**Risk level: LOW in current PAPER mode, HIGH priority for live mode implementation**

---

### Risk 4: Cancellation Complexity on Stop-Outs

**Scenario:** DELTA has a resting limit entry order posted. Simultaneously, the engine decides to stop-out (max_hold, TC exit review). Two conflicting orders: unfilled limit entry + new exit order.

**Impact:** 
- If limit entry hasn't filled, a stop-loss sell has nothing to close → Kalshi API error
- If limit entry filled while stop-loss was being processed → position exists but exit might be misrouted

**Mitigation:**
- Cancel any pending limit entry order before placing exit order
- Add `position_state` machine: `PENDING_ENTRY → OPEN → PENDING_EXIT → CLOSED`
- Gate exit orders on `OPEN` state only

**Risk level: HIGH — must be solved before limit orders can go live**

---

### Risk 5: Fee Calculation Mismatch Post-Implementation

If `fee_calculator.py` is not updated to account for maker (0%) vs taker (7%), the DB `fees_paid` column will overstate fees for maker entries. This corrupts P&L tracking and ECHO grade calculations.

**Mitigation:** Add `order_type` parameter to `calculate_fee()` — if `"maker"`, return 0.0. This is a 2-line change that must accompany any limit order implementation.

**Risk level: LOW (easy fix) but HIGH impact if missed**

---

## Summary and Recommended Sequencing

### The Core Finding

**Fees are not the root cause of any agent's losses — but they are the amplifier.**

| Agent | Root Problem | Fee Contribution | Fee Fix Sufficient? |
|-------|-------------|-----------------|---------------------|
| AXIOM | None — profitable | $20.41 drag on +$46.12 gross | Already profitable; maker entry is upside, not rescue |
| CIPHER | Win/loss asymmetry (0.20x ratio) | $33.00 drag on -$14.87 gross | NO — structural |
| DELTA | Negative gross edge (-$8.65) | $21.86 drag on -$8.65 gross | NO — reduces but doesn't cure |
| DIAMOND | Tiny sample + fee crush | $7.46 drag on -$3.63 gross | MAYBE — maker pricing would reduce from -$11.09 to -$3.63 |
| PHOENIX | 0% win rate | $1.63 drag | NO — edge problem |
| ACE | 16.7% win rate | $1.99 drag | NO — edge problem |

### Recommended Phase

**Do not implement limit orders until live mode is deployed and edge is validated.**

In paper mode, the simulation already assumes instant market fills. Adding limit order simulation complexity creates modeling noise without operational benefit. The right time to implement maker entries is:

1. **Right before first live trade** — implement `passive_limit` path in `order_manager.py` alongside live mode activation
2. **Priority order:** DIAMOND and DELTA first (largest fee drag relative to position size), then AXIOM (upside capture), then CIPHER

### Action Items for David's Decision

| Question | Recommendation |
|----------|---------------|
| Maker entry for DELTA? | YES — 30s timeout + market fallback. $11.34 fee savings with manageable staleness risk at 41-min hold duration. |
| Maker entry for CIPHER? | WAIT — resolve win/loss asymmetry first. Fee savings ($17.74) are meaningful, but CIPHER's gross P&L is -$14.87 regardless. Fix the loss problem first. |
| Maker entry for DIAMOND? | YES, when DIAMOND reaches meaningful sample size. Maker pricing is near-necessary given $2.48/trade fee drag. |
| Maker/maker exits? | YES for target exits (limit at target price is natural). NO for stop-losses (always market). |
| Paper mode fill simulation? | Implement fill probability model before live deployment so expectations are calibrated. |
| Kalshi WS fill events? | Verify before implementing. Determines polling architecture. |
| Min-fill threshold? | Set at 60% — below that, cancel and discard rather than proceed with undersized position. |
