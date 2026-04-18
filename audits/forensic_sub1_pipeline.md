# Forensic Sub-Agent 1: Signal-to-Execution Pipeline Trace
**Generated:** 2026-04-17 ~01:30 UTC  
**Scope:** Silent execution failure since ~01:04 UTC April 17 (8:04 PM CT April 16)  
**Method:** Code reading + log grep analysis — NO code changes made

---

## 1. Pipeline Architecture (from code)

The signal-to-execution pipeline has **5 stages**:

```
[Agent evaluate()] 
    → build_signal()              # edge_pct >= 7.0%, price 25¢–75¢, not benched, not on cooldown
    → submit_signal()             # writes triggers/{name}_signal.json atomically
    → [wake_syndicate.ps1]        # polls triggers/ every 500ms, detects *_signal.json
    → Invoke-AgentSignal()        # calls TC claude CLI (15-30s), gets BUY/PASS JSON
    → writes triggers/{name}_decision.json
    → [main.py decision_loop]     # detects *_decision.json, calls _process_agent_decision()
    → _act_on_decision()          # if decision=="BUY": calls order_manager.place_order()
    → PAPER FILL / TRADE RECORDED
```

**Rules path (scalper engine):**
```
TC writes rules → rules/SCALP/*.json, rules/SWING/*.json, rules/POSITION/*.json
rule_loader._reload() every 60s → in-memory cache
scalper_engine.scan() → get_rules(ticker) → place exit/entry orders
```

**Key chokepoints identified in code:**
- `base_agent.py:303` — hard price gate: `yes_price > 0.75 or yes_price < 0.25` → `return False`
- `base_agent.py:333` — 30-minute per-ticker cooldown per agent
- `base_agent.py:376` — `edge_pct < MIN_EDGE_PCT (7.0%)` → build_signal returns None
- `wake_syndicate.ps1:189` — expiry check: signal older than 5 min → dropped, no TC call
- `wake_syndicate.ps1:263` — TC empty response → signal dropped
- `main.py:154` — sweeper NEVER sweeps `*_signal.json` or `*_decision.json` (confirmed safe)
- `rule_loader.py:43-50` — rules need: ticker, class, entry_price, target_price, stop_price, max_size, expiry

---

## 2. Funnel Numbers (post-cutoff: after 2026-04-17T01:04 UTC)

| Stage | Count | Evidence |
|---|---|---|
| Total log lines after cutoff | 29,710 | `grep -c` |
| Markets scanned per heartbeat | ~1,163 | heartbeat log lines |
| Markets passing liquidity per sweep | ~125-131 | heartbeat log lines |
| Agent evaluations returning "No signal" | 12,541 | `grep -c "No signal"` |
| Signals submitted (agents/scanner) | **415** | `grep "signal submitted"` |
| Entry order attempts | **0** | `grep "ENTRY:\|place_order\|paper.*entry"` |
| Paper fills / New trades | **0** | `grep "PAPER.*fill\|NEW TRADE"` |
| Rules loaded (all intervals) | **0** | `RuleLoader: 0 rules across 0 tickers` |
| TC gate / decision file activity | **0** | no `wake_syndicate`/`decision.json` log hits |
| Errors / Exceptions | **0** | `grep "ERROR\|EXCEPTION\|failed\|traceback"` |

**The funnel drops from 415 signals to 0 entries. Zero rules. Zero TC gate activity.**

---

## 3. Where the Funnel Drops to Zero — KEY FINDING

**Stage: Signal → TC Gate (wake_syndicate.ps1)**

415 signals are successfully written to `triggers/` by agents. Zero are ever processed by `wake_syndicate.ps1`. Zero `{name}_decision.json` files are ever written. Zero `place_order()` calls are made.

### Evidence

**Signals accumulating unprocessed in triggers/:**
```
triggers/ace_signal.json       — LastWriteTime: 4/17/2026 9:12:40 AM (CT)
triggers/axiom_signal.json     — LastWriteTime: 4/17/2026 9:25:55 AM (CT)
triggers/cipher_signal.json    — LastWriteTime: 4/17/2026 9:25:55 AM (CT)
triggers/delta_signal.json     — LastWriteTime: 4/17/2026 9:26:00 AM (CT)
triggers/diamond_signal.json   — LastWriteTime: 4/17/2026 9:25:55 AM (CT)
triggers/ghost_signal.json     — LastWriteTime: 4/17/2026 8:01:10 AM (CT)
triggers/phoenix_signal.json   — LastWriteTime: 4/17/2026 9:09:58 AM (CT)
```
All 7 agent signal files are sitting unread. No `*_decision.json` files exist. This means **wake_syndicate.ps1 is not running**.

**No powershell process running a .ps1 file was found:**
```
Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like '*.ps1*' }
→ No results (only bash.exe and investigation powershell.exe commands returned)
```

**Rules directory is completely empty:**
```
rules/SCALP/   — empty
rules/SWING/   — empty  
rules/POSITION/ — empty
```
`[RuleLoader] Reload complete: 0 rules loaded across 0 ticker(s) (0 active, 0 expired).`
This repeats every 60 seconds indefinitely. No rules have ever been written by TC.

**Status line confirms permanent zero state:**
```
2026-04-17T09:29:54Z INFO syndicate.main — [PAPER] [Status] 2026-04-17T14:29:54Z | positions=0 | session_pnl=$0.00 | exposure=$0.00 | rules=0 (active) | halted=False
```
This pattern repeats from at least `01:09:31Z` onward with no change.

**Last confirmed TC gate activity:** Not found anywhere in the log — there is no evidence the TC gate has EVER processed a signal in the current engine session.

**Last sweeper before cutoff purged 265 files:**
```
2026-04-17T01:09:31Z INFO syndicate.main — [Sweeper] Purged 265 stale trigger files.
```
However, sweeper code at `main.py:154` explicitly exempts `*_signal.json` files from purging. The sweeper is not the culprit. Those 265 were velocity/game_live type files.

**Signals are healthy — sample from around cutoff:**
```
2026-04-17T01:09:09Z INFO syndicate.cipher — [CIPHER] Signal: KXBTCD-26APR1717-T73999.99 YES win_rate=72.2% (n=72) tier=HIGH_CONVICTION
2026-04-17T01:09:09Z INFO syndicate.agents — [AXIOM] signal submitted | ticker=KXBTCD-26APR1717-T73999.99 conviction=HIGH_CONVICTION edge=21.07%
2026-04-17T01:09:10Z INFO syndicate.agents — [DELTA] signal submitted | ticker=KXBTCD-26APR1717-T73999.99 conviction=PROPHECY edge=36.50%
```
Signals are high-quality (edge 21–49%), fully formed, written to disk. None ever get a response.

**Most recent signal sample (ace_signal.json — written ~9:12 AM CT):**
```json
{
  "agent": "ACE",
  "signal": { "ticker": "KXATPMATCH-26APR17FILMUS-FIL", "edge_pct": 48.5, 
              "conviction_tier": "PROPHECY", "expires_at": "2026-04-17T14:17:40Z" }
}
```
By the time forensics ran (~14:30Z), this signal was already expired (14:17:40Z). wake_syndicate.ps1 would have dropped it even if it started now due to `expires_at` check at line 189.

---

## 4. Exact Code Lines of the Collapse Point

**`wake_syndicate.ps1` — the entire script is the collapse point.** It is not running.

The main loop starts at line 498:
```powershell
while ($true) {
    Start-Sleep -Milliseconds 500
    # Code path 2: Agent signal flow — {name}_signal.json
    $AgentSignalFiles = Get-ChildItem -Path $TriggersDir -Filter "*_signal.json" ...
    if ($AgentSignalFiles) {
        Invoke-AgentSignal -File $AgentSignalFiles[0]   # line 592 — never reached
        continue
    }
```

This loop **would** read signals and call `Invoke-AgentSignal()` → TC → write `{name}_decision.json`.

**`main.py` — the decision reader at line 355:**
```python
if fname.endswith("_decision.json") and fname != "decision.json":
    _process_agent_decision(os.path.join(_triggers, fname))  # never called — no files exist
```

**`rule_loader.py:277-335` `_reload()`** — executes every 60s, finds 0 files because:
1. wake_syndicate.ps1 never processes signals into decisions
2. Decisions never call `_act_on_decision()` → `order_manager.place_order()`
3. Rule files are only written by TC via the agent-signal flow; TC never runs

---

## 5. Trigger File Anomalies

**What's in triggers/ right now:**
- 7 `*_signal.json` files — ACCUMULATING, UNPROCESSED, all already expired or near-expiry
- 1 `heartbeat_latest.json` — normal
- 1 `opportunity_scan.json` — normal  
- 1 `strategic_scan.json` — normal
- ~70 `velocity_*.json` files — normal velocity events (swept every 60s but keep regenerating)

**Critical anomaly:** `*_signal.json` files are not swept by main.py (by design — they're TC-facing). But with wake_syndicate.ps1 dead, they simply accumulate. Each scan cycle overwrites the same 7 agent signal files (one per agent, named `{agent}_signal.json`) so the count doesn't grow unboundedly, but they are never consumed.

**No `*_decision.json` files exist in triggers/.** This is definitive proof TC has not processed any signal.

---

## 6. VERDICT

**Signals are dying at Stage 2: the TC Gate (wake_syndicate.ps1) because the process is not running.**

415 signals were submitted to `triggers/` in the post-cutoff window. Zero were picked up. All 7 current agent signal files are sitting unread. Zero `*_decision.json` files have ever been written. Rule directories are empty. The scalper has no rules to execute. Engine shows `rules=0 (active)` every status cycle with no errors — it is healthy but completely starved.

**Root cause:** `wake_syndicate.ps1` is dead. No `.ps1` process was found running on the system. The script is started by `start_syndicate.bat` at launch. Either:
- (a) It was never started in this session, or
- (b) It crashed silently (PS1 errors go to stderr/console, not syndicate.log) and was not restarted

**Why it looks healthy:** main.py, scan_engine.py, all agents, and rule_loader are fully operational. They emit 415 valid signals. The engine logs show no errors. The silence is entirely in the PS1 layer which is external to Python logging.

**To recover:** Restart `wake_syndicate.ps1`. All current signal files are expired — the next scan cycle (within ~5 min) will generate fresh signals that will be consumed immediately once the gate is live.
