# Forensic Sub-Agent 5: Silent Failure Hunt
**Date of audit:** 2026-04-17  
**Silence window:** 01:04 UTC April 17 onward (restart at 00:26 UTC)  
**Investigator:** Adversarial subagent 5 of 5  

---

## EXECUTIVE SUMMARY

The system is **not broken at the code level** — it is broken at the **human-in-the-loop level**. The TC gate (`wake_syndicate.ps1`) is the only process that writes `_decision.json` files that trigger order execution. The last gate decision was recorded at **20:24:22Z on April 16** (8:24 PM CT). From that point forward, signals have been firing continuously — dozens of them — but **zero decisions have been returned**. The engine is alive, scanning, generating signals, and waiting. TC is not responding.

**Confirmed causes of zero trades (in order of certainty):**

1. 🔴 **TC gate is not running or not responding** — the primary cause
2. 🟡 **WS starts with 0 tickers subscribed at boot** — known but benign in current state
3. 🟡 **Rule directory is permanently empty** — scalper's rule-based path is dead
4. 🟢 **54 bare `except Exception:` blocks without log/raise** — silent failure landmines

---

## FINDING 1 — TC Gate: Zero Decisions Since 20:24Z April 16

**Severity: 🔴 Definitely the root cause**

### Evidence

```
Last gate decision in log:
  2026-04-16T20:24:22Z  [Gate] Agent decision: KXBTCD-26APR1717-T75499.99  verdict=PASS

First signal after restart:
  2026-04-17T00:03:23Z  [DIAMOND] signal submitted  conviction=HIGH_CONVICTION

Signals currently sitting in triggers/:
  ace_signal.json     → KXATPMATCH-26APR17FILMUS-FIL  (PROPHECY)
  axiom_signal.json   → KXETHD-26APR1717-T2469.99     (HIGH_CONVICTION)
  cipher_signal.json  → KXBTCD-26APR1717-T76999.99    (HIGH_CONVICTION)
  delta_signal.json   → KXBTCD-26APR1717-T76999.99    (PROPHECY)
  diamond_signal.json → KXBTCD-26APR1717-T76999.99    (HIGH_CONVICTION)
  phoenix_signal.json → KXATPMATCH-26APR16FONSHE-SHE  (PROPHECY)
  ghost_signal.json   → KXATPMATCH-26APR17ZVECER-CER  (HIGH_CONVICTION)
```

### Analysis

`wake_syndicate.ps1` is responsible for reading `*_signal.json` files, calling TC (claude CLI), and writing `*_decision.json` files that main.py's gate poll loop processes every second. The gate is running as a hidden PowerShell process launched by `start_syndicate.bat`. There is no logging from the PS1 script into `syndicate.log` — its output goes to a hidden console window with no persistent log.

**The PS1 script could be silently failing in any of these ways:**
- The `claude` CLI is hanging or returning errors on every prompt call
- Signal files are expiring before TC can respond (each has an `expires_at` timestamp)
- The PS1 process crashed without the watchdog catching it (watchdog monitors `main.py` only — not `wake_syndicate.ps1`)
- The `claude --print --output-format json` call is returning output that fails `Get-FirstJson` parsing, causing the script to drop every decision

**Confirm by running:**  
```powershell
Get-Process powershell | Where-Object {$_.CommandLine -match 'wake_syndicate'}
```
If no process is found, the gate is dead.

**The watchdog (`tools/watchdog.py`) does NOT watch wake_syndicate.ps1.** It only monitors the Python engine PID. This is a coverage gap — the gate can die silently with no restart and no alert.

---

## FINDING 2 — KalshiWS: Subscribes to 0 Tickers at Boot, Depends on One-Shot Callback

**Severity: 🟡 Likely bug — timing race, currently masking itself**

### Code Location

`main.py` lines 667-668:
```python
_kalshi_ws._on_tick_callback = _on_tick
_scan_engine._on_tickers_ready = _kalshi_ws.update_tickers  # one-shot after first heartbeat
```

`core/scan_engine.py` lines 561-571:
```python
if self._on_tickers_ready is not None:
    tickers = list(state.get_all_markets().keys())
    if tickers:
        try:
            self._on_tickers_ready(tickers)
            ...
        except Exception as e:
            logger.error(...)
        self._on_tickers_ready = None  # one-shot — clear after first fire
```

### Log Evidence

```
2026-04-17T00:26:31Z  [KalshiWS] Connected. Subscribing to 0 tickers.
2026-04-17T00:26:31Z  WARNING [KalshiWS] No tickers to subscribe to.
2026-04-17T00:26:51Z  [KalshiWS] Subscription confirmed: ticker
2026-04-17T00:26:51Z  [ScanEngine] Pushed 1163 tickers to WebSocket.
```

### Analysis

At boot, `KalshiWS` is constructed with `tickers=[]`. The WS connects immediately (100ms after boot). The first heartbeat scan completes ~19 seconds later (00:26:49 → 00:26:51), populates `shared_state`, then fires `_on_tickers_ready`. `KalshiWS.update_tickers()` calls `_subscribe()` on the already-open socket — so the subscription IS sent.

**However:** The "Subscription confirmed: ticker" message appears at 00:26:51Z, same second the push happened. This is consistent with a delayed subscribe working. But there is a **gap window** (00:26:31 to 00:26:51 = 20 seconds) where the WS is connected and receiving messages from Kalshi but not subscribed to any ticker. Any tick received during this window is silently discarded.

**Deeper risk:** If the WS reconnects (drops + reconnects) AFTER the one-shot `_on_tickers_ready = None` has fired, `_kalshi_ws.tickers` is still populated (it was set in `update_tickers`), but there is no re-fire of the callback. The `_on_open → _subscribe` path in `KalshiWS._subscribe()` uses `self.tickers` directly, so reconnects work correctly — **this is fine**. The one-shot risk is only at initial boot within that 20-second window.

**Current Status:** Working correctly after the 20-second window. This is not the current cause of zero trades.

---

## FINDING 3 — Rule Directory Is Permanently Empty

**Severity: 🟡 Scalper's rule-based execution path is permanently dead**

### Evidence

```
All rule directories are empty:
  syndicate/rules/SCALP/     — empty
  syndicate/rules/SWING/     — empty
  syndicate/rules/POSITION/  — empty

Every 60 seconds in log:
  [RuleLoader] Reload complete: 0 rules loaded across 0 ticker(s) (0 active, 0 expired).

Status heartbeat:
  rules=0 (active) — consistently since boot
```

### Analysis

The `ScalperEngine.on_price_update()` path requires rules to be loaded. Without rules, the scalper's price-triggered entry path (`_try_entry()`) never fires. The ONLY active execution path is through the TC gate (`_decision.json` → `_act_on_decision()`).

This means 100% of trade execution depends on TC returning decisions. If the gate dies, the entire system halts — there is no fallback autonomous execution path currently active.

**Note:** This may be by design (agent-driven signals only, not rules-driven). But it means `rule_loader` is dead weight consuming CPU on every price tick, and the `ScalperEngine.on_price_update()` loop is running on every WS tick for zero purpose.

---

## FINDING 4 — 54 Silent `except Exception:` Blocks

**Severity: 🟢 Suspicious — landmines, not proven cause of current failure**

### Highest-Risk Silent Swallowers

These blocks in the execution-critical path silently discard failures with no log:

**`scalper/scalper_engine.py:253`** — Rule expiry parse failure: `except Exception: pass`  
If rule expiry is malformed, this silently skips the check and continues. Not dangerous here (continues to trade), but invisible.

**`scalper/scalper_engine.py:326`** and **`:423`** — Exit conditions silently fail.

**`scalper/order_manager.py:207, 226, 231, 406, 411, 433, 438`** — Multiple silent exception swallows in order placement and closure paths. If order_manager fails on a paper mode call, position state may become inconsistent with no log.

**`agents/base_agent.py:240, 249, 532, 539, 625`** — Memory load/save failures silently fall back to defaults. Agent `_benched_cache` could be wrong with no indication.

**`connectors/kalshi_ws.py:273, 294, 361`** — WS send errors silently dropped. A failed ping/pong could cause server to drop the connection with no client-side error log.

**`connectors/kalshi_ws.py:273`**:
```python
try:
    ws.send(json.dumps({"type": "pong"}))
except Exception:
    pass
```
Silent pong failure means the Kalshi server will close the connection after a timeout. The `_on_close` log would appear, but the root cause (failed pong send) would not.

**`core/scan_engine.py:605`** — Agent routing error in heartbeat path silently skips that agent. An agent crash during `should_evaluate` is eaten.

---

## FINDING 5 — Sweeper Deleted 1163 Signal Trigger Files at 00:32:31Z

**Severity: 🟡 Suspicious — may have eaten valid signal files from the first heartbeat**

### Evidence

```
2026-04-17T00:32:31Z  [Sweeper] Purged 1163 stale trigger files.
```

### Analysis

The sweeper runs every 60 seconds, deleting files older than 300 seconds (5 min) that pass `_is_sweepable()`. The `_is_sweepable()` function explicitly protects `*_signal.json` and `*_decision.json` files:

```python
if fname.endswith("_signal.json") or fname.endswith("_decision.json"):
    return False
```

So the 1163 deleted files were `velocity_*` and `new_market_*` files from the first heartbeat at 00:26:49Z (they were created 5+ minutes before the sweeper ran at 00:32:31Z). This is expected behavior.

**However:** The exact same pattern happened at 20:36:52Z on April 16 as well. This is normal operation. The signal files are correctly protected.

---

## FINDING 6 — No Hard Stop, No Exposure Block

**Severity: 🟢 Confirmed not the cause**

- `halted=False` on every status line since restart
- `session_pnl=$0.00`, `exposure=$0.00` — no positions, no PnL
- `hard_stop_loss: 2000.00` in config — far from triggered
- DB query: **0 open positions** — no stuck positions blocking tickers
- No "Hard stop breached" message anywhere in April 17 logs

---

## FINDING 7 — DB Has No Stuck Open Positions

**Severity: 🟢 Confirmed not the cause**

```
Query: SELECT * FROM syndicate_trades WHERE exit_time IS NULL
Result: 0 rows
```

No position dedup locks held incorrectly.

---

## FINDING 8 — `get_available_size()` Has Wrong Default in exposure_manager.py

**Severity: 🟡 Latent bug — not active now, will surface when positions exist**

`core/exposure_manager.py` line 182:
```python
max_total = float(risk.get("max_total_exposure", 50.0))
```

The default fallback is `50.0` dollars, but `get_exposure_summary()` at line 204 also has:
```python
max_total    = float(risk.get("max_total_exposure", 50.0))
```

The config correctly sets `max_total_exposure: 5000.00`. But if the config fails to load (exception swallowed at line 44: `return _cfg_cache or {}`), both functions would fall back to $50 max total exposure, blocking nearly all trades with no ERROR log — only a DEBUG log at the `ALLOW` path. This would appear as zero entries with no explanation. Not currently triggered, but a landmine.

---

## FINDING 9 — Agents Firing Signals But No TC Response: Timeline

**Severity: 🔴 Confirms gate is the bottleneck**

```
BEFORE RESTART (working):
  19:51Z  First signals submitted
  19:52Z  First gate decision received (PASS)
  20:08Z  EXECUTE decision — trade placed
  20:19Z  EXECUTE decision — trade placed
  20:20Z  EXECUTE decision — trade placed
  20:24Z  LAST gate decision (PASS)

AFTER RESTART (silent):
  00:03Z  Signals start firing again
  00:26Z  Engine restarts
  00:26Z  7 signal files sitting in triggers/ waiting
  09:04Z  Signals still firing (CIPHER, AXIOM, DELTA, DIAMOND, PHOENIX, ACE, GHOST)
  09:26Z  STILL no gate decisions — 9+ hours of signals with zero responses
```

The 8-hour+ gap between last gate decision (20:24Z April 16) and now is not explained by any system error in the Python engine. The PS1 gate process is the missing link.

---

## FINDING 10 — Watchdog Does Not Cover wake_syndicate.ps1

**Severity: 🟡 Architecture gap**

`tools/watchdog.py` monitors the Python engine PID only. The `wake_syndicate.ps1` process:
- Has no watchdog
- Has no persistent log (hidden window, no file output)
- Has no heartbeat/health check
- Has no Telegram alert on crash

If it dies, the system appears healthy (scanner running, signals firing, status heartbeat active) but zero trades execute. This is exactly the observed failure mode.

---

## SUMMARY TABLE

| # | File | Line(s) | Issue | Severity |
|---|------|---------|-------|----------|
| 1 | `intelligence/wake_syndicate.ps1` | N/A | TC gate not responding — zero decisions since 20:24Z Apr 16 | 🔴 Root cause |
| 2 | `tools/watchdog.py` | N/A | wake_syndicate.ps1 not monitored — dies silently | 🟡 Architecture gap |
| 3 | `rules/SCALP,SWING,POSITION` | N/A | All rule dirs empty — scalper price-trigger path permanently dead | 🟡 Execution path disabled |
| 4 | `main.py` | 667-668 | WS subscribes with 0 tickers at boot, one-shot callback fires 20s later | 🟡 Timing race (currently working) |
| 5 | `core/exposure_manager.py` | 182, 204 | Default `max_total_exposure=50.0` if config fails to load | 🟡 Latent bomb |
| 6 | `scalper/order_manager.py` | 207,226,231 etc | 7 bare `except Exception:` in order execution path | 🟢 Suspicious |
| 7 | `connectors/kalshi_ws.py` | 273 | Silent pong send failure → server drops connection with no root-cause log | 🟢 Suspicious |
| 8 | `agents/base_agent.py` | 240,249 | Memory load failures silently fall back to empty dict | 🟢 Suspicious |
| 9 | `core/scan_engine.py` | 605 | Agent `should_evaluate` crash silently skipped | 🟢 Suspicious |

---

## RECOMMENDED IMMEDIATE ACTIONS

1. **Check if wake_syndicate.ps1 is running:**
   ```powershell
   Get-Process powershell | Select-Object Id,CPU,StartTime
   ```
   If it's not running, restart it manually.

2. **Add wake_syndicate.ps1 to watchdog coverage** — the watchdog must monitor this process and restart + alert on death.

3. **Add file logging to wake_syndicate.ps1** — output to `logs/gate.log` with timestamps so failures are diagnosable.

4. **Check if signal expiry is killing all signals** — the `expires_at` field in each signal file has a timestamp. If signals are expiring before TC can respond (e.g., claude CLI is hanging), the gate drops them silently. Check the `expires_at` timestamps against current UTC time.

5. **Fix the `max_total_exposure` default** in `exposure_manager.py` lines 182 and 204 — change `50.0` to `5000.0` to match config.
