# Forensic Sub-Agent 3: Agent Health Check
**Investigation window:** Since 01:04 UTC April 17, 2026 (8:04 PM CT April 16)
**Analyst:** Sub-agent 3 of 5
**Generated:** 2026-04-17

---

## 1. Per-Agent Signal Counts Since 01:04 UTC April 17

Counts from `syndicate.agents — [AGENT] signal submitted` log lines between 01:05 UTC and end of log.

| Agent   | Signals After 01:04 UTC | Notes |
|---------|------------------------|-------|
| DIAMOND | 73 | Highest volume; on PROBATION tier ($25 cap) |
| DELTA   | 62 | PROPHECY signals; on PROBATION tier ($25 cap) |
| CIPHER  | 58 | HIGH_CONVICTION; on QUALIFIED tier ($250 cap) |
| AXIOM   | 31 | HIGH_CONVICTION; on QUALIFIED tier ($250 cap) |
| ACE     | 10 | Tennis domain |
| PHOENIX | 6  | |
| GHOST   | 3  | |
| SHADOW  | 0 (no agent-level submissions logged) | Evaluating but passing on all |
| ORACLE  | 0 | Evaluating but passing on all |
| TIDE    | 0 | Evaluating but passing on all |
| BLITZ   | 0 | Memory file missing — may be new/uninitialized |
| MIRROR  | 0 | |
| ENDGAME | 0 | |
| OIL     | 0 | |
| SAGE    | 0 | Sweeping all markets; returning No signal on all |
| ECHO    | 0 | Reporting agent |

**Bottom line:** AXIOM, CIPHER, DELTA, DIAMOND, ACE, PHOENIX, GHOST are all firing signals continuously after 01:04 UTC. The scan loop is healthy. Agents are NOT silent.

---

## 2. Memory File State (Benched/Cooldown Flags)

Checked all memory/*.json files. Results:

| Agent   | benched | loss_streak | benched_until | Notes |
|---------|---------|-------------|---------------|-------|
| AXIOM   | False   | 0           | null          | Clean |
| CIPHER  | False   | 2           | null          | Clean |
| DELTA   | False   | 3           | null          | Clean |
| DIAMOND | False   | 1           | null          | Clean |
| SHADOW  | False   | 0           | null          | Clean |
| ORACLE  | False   | 0           | null          | Clean |
| TIDE    | False   | 0           | null          | Clean |
| MIRROR  | False   | 0           | null          | Clean |
| ECHO    | False   | 0           | null          | Clean |
| SAGE    | False   | 0           | null          | Clean |
| PHOENIX | False   | 3           | null          | Clean (3 losses, not benched yet — bench at 5) |
| GHOST   | False   | 1           | null          | Clean |
| ACE     | False   | 4           | null          | WARNING: 4 loss streak — 1 more loss triggers 4h bench |
| BLITZ   | N/A     | N/A         | N/A           | Memory file MISSING from disk |

**No agent is benched.** No `cooldown_until` or `banned` fields exist in any memory file. The auto-bench threshold is 5 consecutive losses; no agent has reached it.

---

## 3. Base Agent Gate Analysis (`_base_should_evaluate()`)

The gate in `agents/base_agent.py` lines 272–338 checks four conditions, returning False if any fail:

### Gate 1: Bench check
- Reads `_benched_cache` (in-memory bool). Does full file I/O at most once per 60s.
- **Status: CLEAR** — all agents have `benched=False` in memory.

### Gate 2: Contract class filter
- Only allows `SCALP`, `SWING`, `POSITION`. Blocks `WATCH` and unknowns.
- **Status: CLEAR** — logs show agents evaluating SCALP and SWING contracts normally.

### Gate 3: Volume gate
- `market.volume_dollars <= 0` → False.
- **Status: CLEAR** — markets with $0 volume would log "No signal" silently; no impact on active markets.

### Gate 4: Hard price gate (25¢–75¢ sweet spot)
- `YES > 0.75 or YES < 0.25` → False (unless `_skip_base_price_gate = True`).
- **Status: CLEAR** — agents are passing markets in the 25–75¢ range (confirmed in logs: yes_mid=0.71, 0.29, 0.27, 0.36, 0.57, etc.). GHOST has `_skip_base_price_gate = True` to handle out-of-range.

### Gate 5: Re-entry lockout (30-min exit cooldown from `shared_state.exit_lockouts`)
- **Status: POTENTIALLY ACTIVE** — if positions were autonomously exited before 01:04 UTC, their tickers enter a 30-min lockout. The session reset at 01:30 UTC (positions → 0) could have cleared these OR could have left stale lockout state in `shared_state`. This is an in-memory dict, so it resets on restart. **After 01:30 UTC restart, all exit_lockouts cleared** — not a gate after restart.

### Gate 6: Per-ticker 30-min cooldown (`EVAL_COOLDOWN_SECONDS`)
- Persisted to `memory/cooldowns.json` across restarts.
- Default: 1800s (30 min). BLITZ/TIDE override to 300s.
- **Status: NORMAL** — cooldowns.json is large (367KB) and populated but this is expected behavior. After a 30-min cooldown per ticker, agents re-evaluate. This explains why DIAMOND fires ~73 signals across 8+ hours — it's cycling through BTC tickers every 30 min.

### Is net edge (post-fee) being checked vs gross edge?
- `base_agent.py` `MIN_EDGE_PCT = 7.0` applies in `build_signal()` BEFORE signal is written.
- The check is against the passed `edge_pct` parameter. The individual agents compute this — e.g., AXIOM logs `edge=22.2%`, CIPHER logs `edge=33.33%`.
- There is **no post-fee edge check in `_base_should_evaluate()`** — the fee math from Fix 6 would only affect `edge_pct` values passed by individual agents.
- **No evidence that the threshold is blocking signals** — edges of 9-42% are well above the 7% floor.

### Is there a `max_signals_per_cycle` cap silently dropping signals?
- `MAX_SIGNALS_PER_CYCLE` is `None` by default (unlimited). Only DELTA (3) and SHADOW (2) have caps set.
- The buffer+flush mechanism writes the top N by edge_pct after a 2s collection window.
- **Status: Working as designed** — this reduces DELTA's output but doesn't cause a freeze.

---

## 4. AXIOM `evaluate()` Analysis

AXIOM is firing 31 signals after 01:04 UTC. Sample log entry:
```
[AXIOM] evaluate: ticker=KXBTCD-26APR1717-T76999.99 yes_mid=0.71 side=no cost=0.29 edge=22.2% conv=HIGH_CONVICTION days=0
[AXIOM] sizing: conviction=HIGH_CONVICTION base=$100 tier=QUALIFIED cap=$250 → $100
[AXIOM] signal submitted | ticker=KXBTCD-26APR1717-T76999.99 conviction=HIGH_CONVICTION edge=22.17%
```
AXIOM is healthy: computing edge, sizing correctly, submitting signals. Current `axiom_signal.json` shows a KXETHD-26APR1717-T2469.99 YES signal with edge=9.38%, expires_at=2026-04-17T14:30:55Z — a valid, unexpired signal sitting in `triggers/`.

AXIOM uses `hold_to_settlement=True` by design (lines 220-223 of `axiom.py`). This is intentional behavior, not a bug.

**AXIOM's minimum edge threshold has NOT changed** — still uses base `MIN_EDGE_PCT = 7.0` with its own `evaluate()` method computing edge from yes_mid displacement.

---

## 5. Scan Engine Agent Dispatch — Is It Still Running?

The scan engine is demonstrably active:
- Heartbeat scan every ~5 min: `discover_active_series` + `get_all_markets` logged regularly up to 09:31 UTC
- Agent sweep fires on every liquid market: `[Heartbeat] Agent sweep: agent=SAGE ticker=...`
- Agent eval fires on velocity events: `[Heartbeat] Agent eval: agent=AXIOM ticker=...`
- All 16 agents loaded (per scan_engine.py `self._agents` list)

The dispatch pipeline: `should_evaluate()` → `_run_agent_evaluate()` (daemon thread) → `agent.evaluate()` → `build_signal()` → `submit_signal()` → `_write_signal()` → `triggers/{name}_signal.json`

**This pipeline is fully operational.** The logs show `[ScanEngine] [AXIOM] Signal submitted | ticker=...` confirming the trigger file was written and the mtime check confirmed it.

---

## 6. Tier Manager Impact

From logs and tier manager code:
- AXIOM: QUALIFIED tier → `cap=$250` → sizing correctly shows `$100` (conviction < cap)
- DIAMOND: PROBATION tier → `cap=$25` → sizing correctly shows `$25`  
- DELTA: PROBATION tier → `cap=$25` → sizing correctly shows `$25`
- CIPHER: QUALIFIED tier → `cap=$250` → sizing correctly shows `$100`

`get_max_position()` always returns minimum $25 (probation floor hard-coded). After fix C (COALESCE change in tier manager's SQL query), the win detection uses `COALESCE(net_pnl, pnl) > 0`, which is more conservative (net_pnl after fees). This could only REDUCE an agent's tier, not cause a zero return.

**`get_max_position()` is NOT returning 0** — confirmed by sizing logs showing $25/$100 values.

---

## 7. Critical Finding: System Restart + Paper Mode + TC Gate

### The [PAPER] Logging Anomaly
The log contains entries from **two simultaneous processes** with divergent UTC clocks:
- Process A (pre-restart): timestamps in form `2026-04-16T19:xx UTC` printing `[PAPER] [Status] 2026-04-17T00:xx UTC` (5-hour offset)
- Process B (post-restart at ~00:26 UTC): timestamps in `2026-04-17T00:26 UTC` format

A restart occurred at `2026-04-17T00:26:30Z` (confirmed by `[Startup] Triggers cleaned — 0 stale files removed`, all agents re-initialised, `[OutcomeReporter] DB initialised`, `[KalshiWS] Connecting`).

### Why Positions Went to Zero
The pre-restart process showed `positions=3, session_pnl=$-3.61` until approximately 01:29 UTC, then `positions=0, session_pnl=$0.00` at 01:30 UTC. This is the **pre-restart process losing its state** — those existing positions either settled or their state was not visible to the new process.

### The Real Execution Gap: TC Gate Not Processing
The critical missing link:

1. Agents write `triggers/{name}_signal.json` — **CONFIRMED WORKING**
2. `wake_syndicate.ps1` should watch triggers/ and wake TC (Claude CLI) — **NOT CONFIRMED ACTIVE**
3. TC reads signal → writes `{name}_decision.json` — **NO DECISION FILES OBSERVED**
4. `_intelligence_gate_poll_thread` in main.py polls for `*_decision.json` every second — started at `[Gate] Poll thread started` (00:26:31 UTC)
5. `_process_agent_decision()` reads decision → calls `_act_on_decision()` → calls `order_manager.place_order()` → places order

**There is ZERO evidence of any `[Gate]` log entries after startup except `[Gate] Poll thread started`.** No `[Gate] Agent decision:` lines. No `[Gate] Placing order:` lines. No `order_manager` activity.

The trigger files are being written (current `axiom_signal.json` has an unexpired signal from ~09:25 UTC). The gate poll thread is running. But no `{name}_decision.json` files are being dropped back by TC.

### Paper Mode Confirmed
`syndicate_config.yaml` has `paper_mode: true`. This means:
- `order_manager.place_order()` executes paper orders, not live Kalshi API calls
- Paper orders DO appear in the DB with `PAPER-` prefix (confirmed in AXIOM recent_trades: `"order_id": "PAPER-KXBTCD-26APR1617-T73999.99-1776364121"`)
- Paper trades ARE being recorded in `syndicate_trades.db` when orders execute

**The paper/live distinction is irrelevant to the execution freeze** — paper mode still executes "trades" through the same code path, just skipping the Kalshi REST POST.

---

## VERDICT

**"Agents are firing but signals not reaching execution."**

All agents are healthy and emitting signals at full volume:
- 267 signals submitted after 01:04 UTC (AXIOM: 31, CIPHER: 58, DELTA: 62, DIAMOND: 73, ACE: 10, PHOENIX: 6, GHOST: 3)
- Zero agents are benched, on cooldown, or have disabled flags
- `_base_should_evaluate()` has no systemic blocking condition
- Tier manager returns correct non-zero values
- Scan engine is dispatching agents on every scan cycle

**The freeze is at the TC gate bridge layer:**
- `triggers/{name}_signal.json` files are being written and remain present (unprocessed)
- No `{name}_decision.json` files are being created
- The `wake_syndicate.ps1` PowerShell watcher is either not running, not detecting signal file writes, or not successfully invoking Claude CLI
- Without TC decisions, `_process_agent_decision()` never fires, `order_manager.place_order()` is never called, and zero trades execute

**ACE alert:** 4 consecutive losses — one more loss benches ACE for 4 hours.
**BLITZ alert:** Memory file missing from disk — will initialize to default on next startup but loses any historical lessons.

### Recommended investigation for Sub-agents 4 & 5:
- Is `wake_syndicate.ps1` actively running as a file watcher process?
- Is Claude CLI accessible from the PowerShell process (auth token valid)?
- Are there any PS1 or PowerShell error logs showing failed TC wake attempts?
- Check if signal files are being overwritten faster than `wake_syndicate.ps1` can detect them (DIAMOND fires 73 signals — if each overwrites the same `diamond_signal.json`, the PS1 watcher may miss events)
