# DIAMOND Exit Flip-Flop Investigation — 2026-04-17

**Opened:** 2026-04-17 ~11:00 UTC  
**Status:** Root cause confirmed. Fix ready, pending approval.

---

## Section 1: Observed Behavior

DIAMOND position `KXBTCD-26APR1712-T77599.99` (trade id=105) had 8 consecutive exit reviews in ~5 minutes:

| Time (CDT as Z) | Event |
|---|---|
| 10:47:22 | Entry: NO 74x @ 34¢ (YES=66¢) |
| 10:47:32 | Exit review flagged (10s after entry) |
| 10:48:02 | Exit review flagged |
| 10:48:32 | Exit review flagged |
| 10:49:02 | Exit review flagged |
| 10:49:37 | **[Gate] Agent decision: verdict=HOLD — discarding** |
| 10:50:02 | Exit review flagged |
| 10:50:46 | **[Gate] Agent decision: verdict=EXIT — discarding** |
| 10:51:02 | Exit review flagged |
| 10:51:28 | **[Gate] Agent decision: verdict=HOLD — discarding** |
| 10:51:32 | Exit review flagged |
| 10:51:53 | **[Gate] Agent decision: verdict=HOLD — discarding** |
| 10:52:02 | Exit review flagged |
| 10:52:22 | **[Gate] Agent decision: verdict=HOLD — discarding** |
| 10:52:34 | Stop loss -49% — position closed |

Trade result: pnl=-$12.21, net=-$14.90, hold=312s

---

## Section 2: Root Cause

**TC EXIT verdicts have never been wired to close positions.**

### Code path trace:

1. Scalper writes `triggers/diamond_exit.json` every 30s while exit condition holds
2. Gate (`wake_syndicate.ps1`) reads it, calls TC with `exit_prompt.txt`, writes `triggers/diamond_exit_decision.json`
3. `main.py` poll loop (`_decision_poll_thread`, line 354-356) matches `*_decision.json`:
   ```python
   if fname.endswith("_decision.json") and fname != "decision.json":
       _process_agent_decision(os.path.join(_triggers, fname))
   ```
   `diamond_exit_decision.json` matches because it ends with `_decision.json`.

4. `_process_agent_decision()` (line 395-448) normalizes TC verdict:
   ```python
   tc_verdict = decision.get("decision", "PASS").upper()
   verdict = "EXECUTE" if tc_verdict == "BUY" else tc_verdict
   ```
   TC's EXIT verdict stays as "EXIT".

5. The only acting branch:
   ```python
   if verdict == "EXECUTE":
       _act_on_decision(...)
   else:
       logger.info("[Gate] %s — %s: discarding agent signal.", ticker, verdict)
   ```
   **EXIT does not match EXECUTE. It is silently discarded.**

6. No `_process_exit_decision()` function exists anywhere in the codebase.

### Verdict

TC's exit reviews have never had any effect. Every EXIT verdict since the feature was built has been logged as "discarding agent signal" and thrown away. Positions can only close via:
- `_check_time_exits()` — max hold time breach
- `_evaluate_exit()` (hot path) — stop loss or price threshold

---

## Section 3: Is the Flip-Flopping Itself a Problem?

Separately from the wiring bug: TC oscillated HOLD/EXIT/HOLD/HOLD/EXIT/HOLD across 8 reviews.

- **Cause:** Exit reviews are triggered on every WS price tick that meets the review threshold. TC is called with fresh price data each time. If price is bouncing near the exit threshold, TC will flip.
- **TC budget cost:** Each call takes ~28s and hits the Claude API. 8 reviews = ~8 TC calls = ~$0.05-0.10.
- **Is this the intended behavior?** Probably not — the exit review was designed to give TC judgment on borderline exits, not to call TC every 30 seconds for the same position.
- **No hysteresis:** Once a position is flagged for review, it stays in review state every scan cycle. There's no cooldown after TC says HOLD.

This is a secondary issue. The primary bug (EXIT never executes) means the flip-flopping is moot — even if TC said EXIT consistently, nothing would happen.

---

## Section 4: Proposed Fix

**`main.py` — add exit decision handler:**

### Step 1: Exclude exit decisions from `_process_agent_decision`:
```python
# Line 355 — change from:
if fname.endswith("_decision.json") and fname != "decision.json":
# To:
if fname.endswith("_decision.json") and fname != "decision.json" and not fname.endswith("_exit_decision.json"):
```

### Step 2: Add exit decision handler in the same poll loop:
```python
# After the agent decision loop:
for fname in os.listdir(_triggers):
    if fname.endswith("_exit_decision.json"):
        _process_exit_decision(os.path.join(_triggers, fname))
```

### Step 3: Implement `_process_exit_decision()`:
```python
def _process_exit_decision(path: str) -> None:
    """Read and act on a TC exit review decision ({name}_exit_decision.json)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            decision = json.load(f)
        os.remove(path)
    except Exception as e:
        logger.error("[Gate] Could not read/delete exit decision %s: %s", path, e)
        return

    fname     = os.path.basename(path)
    tc_verdict = decision.get("decision", "HOLD").upper()
    ticker    = decision.get("ticker", "UNKNOWN")
    agent     = decision.get("agent", fname.replace("_exit_decision.json", "").upper())

    logger.info("[Gate] Exit decision: %s %s → %s", agent, ticker, tc_verdict)

    if tc_verdict != "EXIT":
        logger.info("[Gate] %s — %s: holding position.", ticker, tc_verdict)
        return

    # TC says EXIT — close the position now
    position = state.get_position(ticker)
    if position is None:
        logger.info("[Gate] %s — EXIT verdict but no open position (already closed).", ticker)
        return

    market = state.get_market(ticker)
    exit_price = market.yes_price if market else None
    from scalper.order_manager import OrderManager
    om = OrderManager()
    om.close_position(ticker, exit_price, reason=f"TC exit review: EXIT verdict ({agent})")
    logger.info("[Gate] %s — TC EXIT executed via order manager.", ticker)
```

**Note:** The `close_position` API call needs verification against `OrderManager`'s actual interface. The above is pseudocode for the pattern.

---

## Section 5: Secondary Fix (exit review hysteresis)

To prevent TC being called on every scan cycle for the same position in review, add a cooldown:

- After TC says HOLD on a position, suppress new exit reviews for that position for 2 minutes
- After TC says EXIT, suppress (position is being closed)
- Implement in `scalper_engine.py` using a `_tc_exit_cooldown: dict[str, float]` mapping ticker → last_review_time

Not urgent — budget impact is small. Do after primary bug fix is validated.

---

## Section 6: Impact Assessment

**Trades affected:** Every trade since exit review feature was built that had a TC EXIT verdict — the exact count is not tracked (TC verdicts are not persisted to DB). Based on today: at least 2 EXIT verdicts observed, both discarded.

**Severity:** Medium-high. The system has a fallback (stop loss + time exit), so positions don't get stuck open forever. But TC's judgment on when to exit is completely ignored, meaning we exit on mechanical rules only, not on market intelligence. This is likely a meaningful P&L drag on any trade where TC correctly identifies an early exit but stop loss hasn't triggered yet.

---

## Status: Ready to Fix

Root cause is unambiguous. Fix is contained to `main.py` (~30 lines). Recommend shipping as Priority 1 before next trading session.

**Do NOT restart engine to deploy** — `main.py` changes require engine restart. Confirm no open positions before restart.
