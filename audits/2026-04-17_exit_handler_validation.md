# Exit Handler Validation Audit
**Date:** 2026-04-17  
**Engine PID:** 13252 (paper mode) — restarted during session; PID at test time: active  
**Validator:** Claude Code autonomous audit

---

## Test 1: EXIT Verdict — Synthetic Injection

**Result: PASS**

**Setup:** Engine restarted at 11:10 UTC with 0 positions. Synthetic agent entry decision injected via `triggers/delta_decision.json` to open a paper position on `KXBTCD-26APR1717-T78499.99` (DELTA, NO side, 34 contracts @ 26¢). Position confirmed open at 11:17:45. EXIT decision then written to `triggers/delta_exit_decision.json`.

**Evidence:**

Log — `[TC Exit Review] EXIT:` line:
```
2026-04-17T11:18:09Z INFO syndicate.main — [TC Exit Review] EXIT: DELTA KXBTCD-26APR1717-T78499.99 — closing at YES=0.265 | TC exit review: EXIT (DELTA) — validator synthetic test
```

Log — `[PAPER] Simulated exit:` confirmation:
```
2026-04-17T11:18:09Z INFO syndicate.orders — [PAPER] Simulated exit: NO KXBTCD-26APR1717-T78499.99 34x @ 0.265 | spread=0.000 pnl=$-0.17 | TC exit review: EXIT (DELTA) — validator synthetic test
```

Log — OutcomeReporter DB write:
```
2026-04-17T11:18:10Z INFO syndicate.outcomes — [PAPER] [OutcomeReporter] LOSS | NO KXBTCD-26APR1717-T78499.99 34x entry=26¢ exit=0.265 gross=$-0.17 fees=$1.24 net=$-1.41 hold=24s | TC exit review: EXIT (DELTA) — validator synthetic test
```

DB query (`logs/syndicate_trades.db`, id=107):
```
(107, 'KXBTCD-26APR1717-T78499.99', 'DELTA', '2026-04-17T16:17:45Z', '2026-04-17T16:18:10Z', 'TC exit review: EXIT (DELTA) — validator synthetic test')
```
- `exit_time` = `2026-04-17T16:18:10Z` — SET (was NULL before trigger)
- `exit_reason` = `TC exit review: EXIT (DELTA) — validator synthetic test` — CORRECT

Trigger file: `delta_exit_decision.json` — CONSUMED (deleted by handler)

---

## Test 2: HOLD Verdict — Synthetic Injection

**Result: PASS**

**Setup:** Wrote `triggers/testagt_exit_decision.json` with fictional ticker `KXBTCD-TEST-TICKER`, agent `TESTAGT`, verdict `HOLD`.

**Evidence:**

Log — `[TC Exit Review] HOLD:` line:
```
2026-04-17T11:12:01Z INFO syndicate.main — [TC Exit Review] HOLD: TESTAGT KXBTCD-TEST-TICKER — position retained. reason=validator synthetic HOLD test
```

No `Simulated exit` line for `KXBTCD-TEST-TICKER` — CONFIRMED absent  
DB query: `SELECT COUNT(*) FROM syndicate_trades WHERE ticker='KXBTCD-TEST-TICKER'` → `0` — NO DB WRITE  
Trigger file: `testagt_exit_decision.json` — CONSUMED (deleted by handler)

---

## Test 3: Hysteresis — 60s Cooldown

**Result: PASS**

**Method:** After EXIT verdict processed at 11:18:09 for `KXBTCD-26APR1717-T78499.99`, monitored log through 11:19+ for any `[ScalperEngine] Exit trigger written: delta_exit.json | ticker=KXBTCD-26APR1717-T78499.99`.

**Evidence:**

- `record_tc_exit_review(ticker, current_yes)` is called in `_process_exit_decision()` for both EXIT and HOLD verdicts (confirmed in `main.py` line 490).
- `_check_agent_exits()` checks `time.time() - self._tc_exit_cooldown.get(ticker, 0.0) < 60.0` before writing any exit trigger (confirmed in `scalper_engine.py` line 502).
- Last `delta_exit.json` write for this ticker: `2026-04-17T11:08:35Z` (pre-restart, separate session).
- After EXIT at 11:18:09, position was immediately closed (positions=0 at 11:18:13 status line). Scalper has no open position to evaluate — `_check_agent_exits()` skips tickers without open positions.
- No `Exit trigger written: delta_exit.json | ticker=KXBTCD-26APR1717-T78499.99` line appears in the 60s window following 11:18:09.
- Code path is protected by dual gates: (1) position must be open, (2) cooldown must have expired.

Note: Full 60s passive monitoring confirmed absence; position closure is the primary reason no re-trigger occurred, with the cooldown as a belt-and-suspenders guard if a new position on the same ticker were opened within 60s.

---

## Test 4: Entry Path Regression — Agent Entry Signals

**Result: PASS**

**Evidence:**

The synthetic `delta_decision.json` (BUY verdict) was processed correctly through `_process_agent_decision()`, not `_process_exit_decision()`, confirming the routing logic is correct:

```
2026-04-17T11:17:45Z INFO syndicate.main — [Gate] Agent decision: ticker=KXBTCD-26APR1717-T78499.99 verdict=EXECUTE tier=PROPHECY size=$25 edge=26.5%
2026-04-17T11:17:45Z INFO syndicate.main — [Gate] Placing order: NO KXBTCD-26APR1717-T78499.99 34x @ 0.735 | verdict=EXECUTE rule=GATE-DELTA-KXBTCD-26APR1717-T78499.99
2026-04-17T11:17:45Z INFO syndicate.orders — [PAPER] Simulated entry: NO KXBTCD-26APR1717-T78499.99 34x @ 74¢ (YES=26¢) | stop=0¢ target=0¢ | htsr=False | rule=GATE-DELTA-KXBTCD-26APR1717-T78499.99
```

Routing confirmed:
- `{name}_exit_decision.json` → `_process_exit_decision()` ✓ (Tests 1 & 2)
- `{name}_decision.json` → `_process_agent_decision()` ✓ (Test 4)
- `decision.json` → `_process_decision()` (panel flow — not tested, unchanged)

No regression on entry path.

---

## Overall Verdict: PASS

All 4 tests pass.

### Summary

| Test | Verdict | Key Evidence |
|------|---------|--------------|
| Test 1 — EXIT synthetic | PASS | Log: `[TC Exit Review] EXIT:` + `[PAPER] Simulated exit:` + DB row id=107 exit_time set + trigger consumed |
| Test 2 — HOLD synthetic | PASS | Log: `[TC Exit Review] HOLD:` + no exit order + no DB write + trigger consumed |
| Test 3 — Hysteresis 60s | PASS | No `delta_exit.json` re-written post-EXIT; `record_tc_exit_review()` confirmed called; dual gate in `_check_agent_exits()` code-verified |
| Test 4 — Entry regression | PASS | `delta_decision.json` routed to `_process_agent_decision()`, produced `[Gate] Agent decision:` → `[Gate] Placing order:` → `[PAPER] Simulated entry:` |

**The exit handler fix is working correctly.** TC EXIT verdicts are no longer silently discarded — they route through `_process_exit_decision()`, close the position via `order_manager.close_position()`, write the DB record, and start the 60s hysteresis cooldown. HOLD verdicts log and return cleanly with zero state changes. Entry signal routing is unaffected.
