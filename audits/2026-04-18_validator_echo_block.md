# Validator: ECHO block live gate test
**Date:** 2026-04-18  
**Gate PID:** 26776 (started with stdout/stderr → wake_syndicate.log)  
**echo_blocks.json:** Seeded with CIPHER:KXBTCD:40-60 block for trade #108

## Setup

```json
{
  "CIPHER:KXBTCD:40-60": {
    "blocked_until": "2026-04-18T22:34:00Z",
    "reason": "F grade: pnl=$-48.38 on CIPHER KXBTCD 40-60 — manually written post-trade #108",
    "source_trade_id": 108,
    "created_at": "2026-04-18T18:29:28Z"
  }
}
```

## PowerShell block-logic isolation test

Confirmed PSObject.Properties["CIPHER:KXBTCD:40-60"] lookup works with colon-containing keys:
- `BlockEntry found` ✅
- `blocked_until: 4/18/2026 10:34:00 PM` (UTC)
- `UtcNow: 4/18/2026 6:41:31 PM`
- `Still blocked: True` ✅

## Test 3a — Blocked signal (KXBTCD 40-60 bucket)

**Signal:** `cipher_signal.json` — ticker=KXBTCD-26APR1817-T48000, entry_price=0.50

**Gate log output:**
```
[Syndicate Gate] Agent signal: cipher_signal.json | agent=CIPHER
[Syndicate Gate] [ECHO BLOCK] CIPHER KXBTCD 40-60 blocked until 2026-04-18T22:34:00Z -- dropping: F grade: pnl=$-48.38 on CIPHER KXBTCD 40-60 - manually written post-trade #108 (gap-down stop loss at 165 contracts unvalidated scale)
```

**Signal file:** Consumed (deleted). No `cipher_decision.json` written. TC NOT invoked.

**Result: BLOCKED correctly** ✅

## Test 3b — Pass-through signal (KXBTCD 60-80 bucket)

**Signal:** `cipher_signal.json` — ticker=KXBTCD-26APR1817-T48000, entry_price=0.70

**Gate log output:**
```
[Syndicate Gate] Agent signal: cipher_signal.json | agent=CIPHER
[Syndicate Gate] Invoking TC for CIPHER (max 120s)...
[Syndicate Gate] Decision written: cipher_decision.json
[Syndicate Gate] Agent flow done for CIPHER.
```

**Result: NOT blocked, TC invoked normally** ✅

## Result: PASS — ECHO block correctly fires on 40-60 and passes through 60-80
