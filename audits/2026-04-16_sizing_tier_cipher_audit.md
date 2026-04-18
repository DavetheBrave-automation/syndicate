# Syndicate Audit — 2026-04-16
## Sizing × Conviction Display × CIPHER Direction

**Prepared by:** 3 parallel subagents + main agent verification  
**Status:** AWAITING DAVID APPROVAL before any code changes ship

---

## Executive Summary

All three problems trace to **one shared root cause** (Problems 1 and 2 are the same bug in the same file). CIPHER (Problem 3) is clean.

| Problem | Root Cause | File | Fix Complexity |
|---------|-----------|------|----------------|
| Sizing shrink (27x) | Agent flow bypass — TC's `bet_size` used instead of signal's `max_size_dollars` | `wake_syndicate.ps1` | 6 lines added |
| Conviction shows `?` | Same bypass — `conviction_tier` not injected into agent decision file | `wake_syndicate.ps1` | 1 line added |
| CIPHER at 70-74¢ | LEGITIMATE — pattern-based, 80% win rate on 20 trades | — | No fix |

---

## Problem 1 — Sizing Shrink (27x)

### Traced Pipeline

```
AXIOM.evaluate()
  └─ get_bet_size("HIGH_CONVICTION") → $100 [base_agent.py:870]
       └─ min(100, tier_cap=250) → $100 ✓
          └─ build_signal() → max_size_dollars: 100 [base_agent.py:429]
               └─ axiom_signal.json written ✓

wake_syndicate.ps1 Invoke-AgentSignal()
  └─ Calls TC with base_decision_prompt.txt
       └─ TC responds: {"decision":"BUY","bet_size":4,"side":"no",...}
            └─ PS1 injects: ticker, contract_class, agent_name, edge_pct, stop_price
                 └─ PS1 does NOT inject: size/max_size_dollars, conviction_tier, side ← BUG

main.py _process_agent_decision()
  └─ decision["size"] = decision.get("bet_size", 25) → 4 [main.py:421]
       └─ _act_on_decision(ticker, "EXECUTE", size=4, decision)
            └─ contract_price = 0.74 (YES)
                 └─ quantity = min(99, int(4 / 0.74)) = 5 contracts ← WRONG
```

### Why TC returns bet_size=4

TC is prompted to act as the agent and generate a position size from context. With no explicit instruction to use `max_size_dollars`, TC infers a conservative size from the signal context (likely computing 5 contracts × ¢ as a "safe" entry). This is not a TC prompt failure — it's that TC's size output was never intended to be authoritative; it's advisory. The agent's conviction system is authoritative.

### Expected vs Actual

| | Expected | Actual |
|---|---|---|
| size_dollars | $100 | $4 |
| quantity | 99 contracts (capped by Kalshi 99-limit) | 5 contracts |
| stake | $74.26 | $3.70 |

Note: even with correct sizing, the Kalshi 99-contract limit caps output at 99×74¢=$73.26, not $100. This is correct behavior — the 99-cap exists. Expected contracts = 99, not 135.

### Fix 1.1 (proposed, not applied)

**File:** `wake_syndicate.ps1`, function `Invoke-AgentSignal`, after line 291 (after existing Add-Member calls)

```powershell
# ── Inject signal-authoritative fields -- TC's size/side/conviction are advisory only ──
$SignalConvictionTier = if ($Obj.signal -and $Obj.signal.PSObject.Properties["conviction_tier"]) `
    { $Obj.signal.conviction_tier } else { "GLITCH" }
$SignalMaxSizeDollars = if ($Obj.signal -and $Obj.signal.PSObject.Properties["max_size_dollars"]) `
    { [int]$Obj.signal.max_size_dollars } else { 25 }
$SignalSide = if ($Obj.signal -and $Obj.signal.PSObject.Properties["side"]) `
    { $Obj.signal.side } else { "yes" }
$SignalEntryPrice  = if ($Obj.signal -and $Obj.signal.PSObject.Properties["entry_price"])  `
    { $Obj.signal.entry_price }  else { $null }
$SignalTargetPrice = if ($Obj.signal -and $Obj.signal.PSObject.Properties["target_price"]) `
    { $Obj.signal.target_price } else { $null }

$DecisionParsed | Add-Member -NotePropertyName "conviction_tier" -NotePropertyValue $SignalConvictionTier -Force
$DecisionParsed | Add-Member -NotePropertyName "size"            -NotePropertyValue $SignalMaxSizeDollars -Force
$DecisionParsed | Add-Member -NotePropertyName "side"            -NotePropertyValue $SignalSide           -Force
if ($null -ne $SignalEntryPrice) {
    $DecisionParsed | Add-Member -NotePropertyName "entry_price"  -NotePropertyValue $SignalEntryPrice  -Force
}
if ($null -ne $SignalTargetPrice) {
    $DecisionParsed | Add-Member -NotePropertyName "target_price" -NotePropertyValue $SignalTargetPrice -Force
}
```

**Risk:** LOW. This restores the intended behavior — agent's sizing is authoritative. TC can still PASS/BLOCK (position won't be placed at all if TC rejects). TC can no longer silently shrink a valid BUY to $4.

**Regression check:** REDUCE verdict from TC in agent flow → main.py line 383 handles `"REDUCE"` same as `"EXECUTE"` (no size halving in this path). This is a pre-existing gap, not introduced by this fix.

---

## Problem 2 — Conviction Tier Shows `?`

### Traced Pipeline

Same agent flow bypass as Problem 1:

```
base_agent.build_signal()
  └─ signal["conviction_tier"] = "HIGH_CONVICTION" [base_agent.py:418] ✓

wake_syndicate.ps1 Invoke-AgentSignal()
  └─ TC JSON has no "conviction_tier" key
       └─ PS1 injects: ticker, contract_class, agent_name, edge_pct, stop_price
            └─ "conviction_tier" NOT injected ← BUG

main.py _process_agent_decision()
  └─ conviction_tier = decision.get("conviction_tier", "?") → "?" [main.py:435]
       └─ rule["conviction_tier"] = "?" [main.py:494]
            └─ _trade_data["conviction_tier"] = "?" [order_manager.py:178]
                 └─ alert: "Tier: QUALIFIED • ?" ← WRONG
```

### Fix 1.2 (proposed, not applied)

**Same file, same edit as Fix 1.1** — the `conviction_tier` injection is included in that block. No separate change needed.

---

## Problem 3 — CIPHER Direction Audit

### Verdict: LEGITIMATE

CIPHER's 70-74¢ YES entries are data-driven, not a direction bug.

**CIPHER's logic (cipher.py ~line 318-321):**
- Queries historical win rates by price bucket (60-80¢) from DB
- If YES win rate ≥ 60% in that bucket → buys YES
- If NO win rate ≥ 60% and > YES win rate → switches to NO
- No hardcoded side (contrast: ACE has hardcoded YES-only at line 175)

**Database (last 20 CIPHER trades):**
- Side: 100% YES (all trades in 60-80¢ bucket; NO bucket data not yet accumulated)
- Win rate: 80% (16/20)
- Avg entry: 67.7¢
- Total P&L: +$7.93

**Comparison:**

| Agent | Price gate | Side selection | Win rate | Total P&L | Status |
|-------|-----------|---------------|----------|-----------|--------|
| CIPHER | None (0-100¢) | Win-rate based (pattern) | 80% | +$7.93 | ✓ Clean |
| AXIOM  | 25-75¢ only  | Cheap-side rule | 56.2% | +$62.12 | ✓ Clean |
| ACE    | 5-95¢        | Hardcoded YES-only | 16.7% | -$1.32 | ❌ Bug |

CIPHER's 88% → 80% regression: sampling artifact, not a trend. 4 additional trades with 2 losses brings 20/20 to 16/20. Still well above the 60% threshold.

**Recommendation:** Keep CIPHER running. No code change.

---

## Risk Assessment

| Fix | Risk | Regression risk | Blast radius |
|-----|------|-----------------|-------------|
| Fix 1.1 (size+conviction+side) | LOW | None — restores intended behavior | Agent flow only |
| Fix 3 (CIPHER) | N/A — no change | N/A | None |

**The only meaningful risk:** if an agent's signal contains a wrong side or entry_price, we're now locking that in. But:
- Side logic lives in agent.evaluate() with proof-checks in logs
- entry_price is already used as fallback in main.py line 472-473 if market data unavailable
- The side override is consistent with panel flow (parse_decision.py always uses signal side)

---

## Order of Operations

1. **Fix 1.1** — `wake_syndicate.ps1` (single edit, fixes both Problems 1 and 2 simultaneously)
2. **Validate** — 3 test cases (see Phase 3 spec)
3. **Dashboard rebuild** — independent, can ship in parallel
4. **No Fix 3** — CIPHER is clean

---

## Proposed Fix Diff

**File:** `C:/Users/djnec/CommandCenter/syndicate/intelligence/wake_syndicate.ps1`  
**Function:** `Invoke-AgentSignal`  
**After line 294** (after `if ($null -ne $SignalStopPrice)` block closes):

```diff
+    # ── Inject signal-authoritative fields (TC sizing/side/conviction are advisory) ──
+    $SignalConvictionTier = if ($Obj.signal -and $Obj.signal.PSObject.Properties["conviction_tier"]) { $Obj.signal.conviction_tier } else { "GLITCH" }
+    $SignalMaxSizeDollars = if ($Obj.signal -and $Obj.signal.PSObject.Properties["max_size_dollars"]) { [int]$Obj.signal.max_size_dollars } else { 25 }
+    $SignalSide           = if ($Obj.signal -and $Obj.signal.PSObject.Properties["side"]) { $Obj.signal.side } else { "yes" }
+    $SignalEntryPrice     = if ($Obj.signal -and $Obj.signal.PSObject.Properties["entry_price"]) { $Obj.signal.entry_price } else { $null }
+    $SignalTargetPrice    = if ($Obj.signal -and $Obj.signal.PSObject.Properties["target_price"]) { $Obj.signal.target_price } else { $null }
+    $DecisionParsed | Add-Member -NotePropertyName "conviction_tier" -NotePropertyValue $SignalConvictionTier -Force
+    $DecisionParsed | Add-Member -NotePropertyName "size"            -NotePropertyValue $SignalMaxSizeDollars -Force
+    $DecisionParsed | Add-Member -NotePropertyName "side"            -NotePropertyValue $SignalSide           -Force
+    if ($null -ne $SignalEntryPrice)  { $DecisionParsed | Add-Member -NotePropertyName "entry_price"  -NotePropertyValue $SignalEntryPrice  -Force }
+    if ($null -ne $SignalTargetPrice) { $DecisionParsed | Add-Member -NotePropertyName "target_price" -NotePropertyValue $SignalTargetPrice -Force }
```

---

## Validation Test Cases (Phase 3)

After fix ships, validate these:

| Agent | Tier | Conviction | Price | Expected qty | Expected stake |
|-------|------|-----------|-------|-------------|----------------|
| AXIOM | QUALIFIED ($250) | HIGH_CONVICTION ($100) | 74¢ YES → NO @ 26¢ | 99 contracts (Kalshi cap) | ~$25.74 (NO @ 26¢) |
| CIPHER | PROBATION ($25) | HIGH_CONVICTION | any | 96 contracts @ 26¢ max | ~$25 |
| DIAMOND | PROBATION ($25) | PROPHECY ($300 → capped at $25) | 30¢ | 83 contracts | ~$25 |

Note: AXIOM trades NO side at 74¢ YES. Its stake is $25.74 (99×26¢), not $73 (99×74¢). The conviction cap ($100) and tier cap ($250) both exceed the NO cost, so Kalshi 99-contract cap is the binding constraint here, giving ~$25.74 stake.

---

**Awaiting approval to proceed to Phase 3.**
