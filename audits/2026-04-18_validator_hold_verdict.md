# Validator: HOLD verdict E2E
**Date:** 2026-04-18  
**Status: DEFERRED — engine not started for restart**

## Why Deferred

Test 6 (HOLD verdict) requires:
1. A running engine with an open position
2. Writing an `*_exit_decision.json` with `{"decision": "HOLD", ...}`
3. Confirming position stays open, no closed DB row, no fee charges

The engine is currently stopped pending David's restart approval. Starting the engine briefly for this test risks:
- New trades opening before tests complete
- Contaminating the clean pre-restart state

## Plan for Post-Restart Validation

On first natural open position after restart:
1. Identify the position ticker
2. Manually write `{ticker}_exit_decision.json` with `{"decision": "HOLD", "agent": "...", "reasoning": "HOLD verdict test", "ticker": "..."}`
3. Wait 60 seconds
4. Confirm: position still in `state.get_all_positions()`, no closed DB row, log shows `[TC Exit Review] HOLD: ... position retained`

## Partial Evidence (EXIT verdict)

EXIT verdict was confirmed real in the pre-restart sprint (ids 109, 110, 111 — all TC exit reviews with correct DB writes, fees, and exit_times). The HOLD path is the else-branch in `_process_exit_decision`:

```python
if tc_verdict != "EXIT":
    logger.info("[TC Exit Review] HOLD: %s %s — position retained.", agent, ticker, ...)
    return  # no DB write, position stays open
```

Code is simple and deterministic. HOLD verdict test deferred to first natural position post-restart.

## Risk Assessment

LOW — the HOLD path is a no-op (early return). The only failure mode would be a corrupted JSON in the decision file, which would raise an exception in `_process_exit_decision` and log an error. This doesn't open or close any position. The EXIT path (which closes positions) was validated live on 3 real positions.
