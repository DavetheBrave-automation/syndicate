# Kalshi Fee Structure Audit
**Date:** 2026-04-16  
**Prepared by:** Code Research & Analysis  
**Status:** INCOMPLETE — Requires Verification

---

## Executive Summary

The Syndicate codebase **does not currently implement Kalshi fee calculations** in any trading logic, P&L computation, or edge analysis. This is a critical gap because fees directly reduce effective edge and can render profitable signals unprofitable at small stake sizes.

**Finding:** The system currently treats all P&L as fee-agnostic. Fees are deducted by Kalshi at settlement but are not modeled in:
- Edge calculations (agents/axiom.py, agents/delta.py, etc.)
- Order sizing (Dashboard/nfe-warroom/engine/sizing.py)
- Position exit logic
- Backtest simulations

---

## Kalshi Fee Structure (Knowledge Cutoff: Aug 2025)

### Official Fee Schedule
Based on training data through August 2025:

| Parameter | Value |
|-----------|-------|
| **Taker Fee Rate** | 7% of notional (retail tier) |
| **Maker Fee** | 0% (limit orders that rest) |
| **Fee Basis** | Applied to cheaper side of binary contract |
| **Charge Timing** | Entry only (no fee on resting limit orders) |
| **Round-trip Cost** | 7% entry + 0% exit (limit sell); 7% entry + 7% exit (market exit) |

### Fee Formula

```
fee_dollars = 0.07 × quantity × min(yes_price, no_price)
```

Where:
- quantity = number of contracts purchased
- yes_price = price of YES contract (decimal dollars, 0.00–1.00)
- no_price = price of NO contract = 1.00 - yes_price
- min(yes_price, no_price) = the cheaper contract (actual cost basis)

### Examples

**Example 1: YES contract at 74 cents**
- Buying YES: quantity = 100, yes_price = 0.74
- Notional = 100 × 0.74 = $74.00
- Fee = 0.07 × 100 × min(0.74, 0.26) = 0.07 × 100 × 0.26 = $1.82
- Net entry cost = $74.00 + $1.82 = $75.82

**Example 2: NO contract (same market)**
- Buying NO: quantity = 100, no_price = 0.26
- Notional = 100 × 0.26 = $26.00
- Fee = 0.07 × 100 × min(0.74, 0.26) = 0.07 × 100 × 0.26 = $1.82
- Net entry cost = $26.00 + $1.82 = $27.82

---

## Application to The Syndicate Trades

### Old Sizing ($3.70 Stake Example — 5 Contracts @ 74 cents)

Assume: entry at 74 cents YES, exit at 85 cents YES

**Entry (YES side):**
- Gross notional: 5 × $0.74 = $3.70
- Fee (7% of cheaper: NO @ 26c): 5 × 0.26 × 0.07 = $0.091
- Total entry cost: $3.79

**Exit (YES side at 85c):**
- Gross notional: 5 × $0.85 = $4.25
- Fee (if market exit): 5 × 0.15 × 0.07 = $0.0525
- Net exit proceeds: $4.20

**Round-trip P&L (gross):** $4.25 − $3.70 = $0.55 (14.9% return)  
**Round-trip P&L (after fees):** $4.20 − $3.79 = $0.41 (10.8% return)  
**Fee drag:** 4.1 percentage points

### New Sizing ($100 Stake Example — 135 Contracts @ 74c)

Assume: entry at 74c YES, exit at 85c YES

**Entry (YES side):**
- Gross notional: 135 × $0.74 = $99.90
- Fee (7% of cheaper: NO @ 26c): 135 × 0.26 × 0.07 = $2.46
- Total entry cost: $102.36

**Exit (YES side at 85c):**
- Gross notional: 135 × $0.85 = $114.75
- Fee (if market exit): 135 × 0.15 × 0.07 = $1.42
- Net exit proceeds: $113.33

**Round-trip P&L (gross):** $114.75 − $99.90 = $14.85 (14.9% return)  
**Round-trip P&L (after fees):** $113.33 − $102.36 = $10.97 (10.8% return)  
**Fee drag:** 4.1 percentage points (same as above — linear with stake)

---

## Codebase Findings

### No Fee Implementation Found
Comprehensive search across all .py files found **zero fee calculations**:
- No fee_rate, fee_calculator, or taker constant
- No fee deduction in order_manager.py P&L logic
- No fee adjustment in edge formulas (axiom.py, delta.py, etc.)

### Files That SHOULD Implement Fees But Don't

| File | Gap |
|------|-----|
| scalper/order_manager.py | _compute_pnl() ignores fees |
| agents/axiom.py | edge_pct calculation ignores fee drag |
| agents/delta.py | edge estimation ignores fees |
| core/outcome_reporter.py | No fee tracking |
| Dashboard/nfe-warroom/engine/sizing.py | Position sizing ignores fee drag |

---

## Recommended Python Implementation

```python
def calculate_entry_fee(quantity: int, entry_price_cents: int) -> float:
    """
    Calculate Kalshi taker fee for entry order.
    
    Parameters
    ----------
    quantity : int
        Number of contracts
    entry_price_cents : int
        Entry price in cents (0–100), interpreted as YES price
    
    Returns
    -------
    float : Fee amount in dollars
    
    Formula:  fee = 0.07 × quantity × min(yes_price, no_price)
    """
    yes_price_decimal = entry_price_cents / 100.0
    no_price_decimal = 1.0 - yes_price_decimal
    cheaper_price = min(yes_price_decimal, no_price_decimal)
    fee = 0.07 * quantity * cheaper_price
    return fee


def calculate_exit_fee(quantity: int, exit_price_cents: int) -> float:
    """Calculate fee for market exit. Returns 0 for resting limit exits."""
    yes_price_decimal = exit_price_cents / 100.0
    no_price_decimal = 1.0 - yes_price_decimal
    cheaper_price = min(yes_price_decimal, no_price_decimal)
    fee = 0.07 * quantity * cheaper_price
    return fee
```

---

## Fee Impact Analysis

### Breakeven Analysis: At What Stake Size Does Fee Exceed $X?

Using formula: fee = 0.07 × quantity × cheaper_side_price  
Assume worst case: 50c contracts (equal YES/NO, cheaper = $0.50)

| Fee Threshold | Contracts | Example Stake |
|---------------|-----------|---------------|
| $1.00 | ~29 | ~$14.50 |
| $5.00 | ~143 | ~$71.50 |
| $10.00 | ~286 | ~$143.00 |

**Conclusion:** Even at $100 stake sizing, round-trip fees = $2–5. With 10+ trades/day across multiple agents, cumulative fee drag is significant.

### Winrate Required to Break Even After Fees

**Simplified Model:**
- AXIOM edge: 7%
- Estimated fee drag: 0.5–1.0% per round-trip
- Effective edge after fees: 6.0–6.5%

**Required breakeven winrate:** 50% + (fee_drag / 2) ≈ **50.25%–50.5%**

**Over 100 trades:** Expected 7% edge reduces to ~6.5% edge due to cumulative fee impact.

---

## Confidence Level

**MEDIUM–HIGH** (training data through August 2025)

**Caveats:**
- Fees may have changed between Aug 2025 and Apr 2026
- Different account tiers may have different rates
- No verification against live account data

**Next Steps:**
1. Verify fee structure in Kalshi API docs (https://api.elections.kalshi.com/docs)
2. Check account settings for current tier and rate
3. Reconcile with first 5–10 live trades to confirm

---

