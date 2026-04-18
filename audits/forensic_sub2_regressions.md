# Forensic Sub-Agent 2 — Fix Regression Audit
**Generated:** 2026-04-17 ~14:30 UTC  
**Scope:** Fixes 1–6 + A–D shipped before/during the execution freeze  
**Freeze onset:** ~01:04 UTC April 17 (positions drained to 0, rules=0, no entries since)

---

## CRITICAL FINDING (not a regression — pre-existing condition confirmed)

**The RuleLoader has shown 0 rules across ALL scans since at least 19:52 UTC April 16.**  
The scalper_engine hot path (`on_price_update → _evaluate_entry`) requires rules from `rule_loader.get_rules(ticker)`. With 0 rules, no rule-based entries ever fire. This is NOT caused by any of the fixes below — the `rules/SCALP/`, `rules/SWING/`, and `rules/POSITION/` directories are all empty on disk right now.

**The actual entry path that was working** is the TC Gate flow: agents write `{name}_signal.json` → wake_syndicate.ps1 → `{name}_decision.json` → `_process_agent_decision()` → `_act_on_decision()` → `place_order()`.

**What happened at 01:04 UTC:** Syndicate restarted (log shows `[Gate] Poll thread started` at 00:26, and at 01:04:31 the Sweeper ran `[Sweeper] Purged 11 stale trigger files` and session P&L reset to $0.00). After restart, no `_decision.json` files were produced — meaning either TC/wake_syndicate.ps1 was not running, or signal files expired before PS1 processed them. The agents ARE generating signals (confirmed in log through 09:30), but there is no `[Gate] Agent decision:` log entry anywhere on April 17, and no `[Gate] Placing order:` entry.

**Conclusion: The freeze is a Gate pipeline break — wake_syndicate.ps1 is either not running or not processing signals.** None of Fixes 1-6/A-D caused this.

---

## Fix 2 — Order Chunking (`scalper/order_manager.py`)

**VERDICT: CLEAN**

Audit findings:
- `_compute_chunks(0)` returns `[]` (while loop never executes). However, `quantity` can never be 0 at call time: in `place_order()`, `quantity = min(quantity, max_size)` where quantity is already passed in. In `_evaluate_entry()` (scalper_engine.py line 281): `quantity = max(1, int(proposed_dollars / new_price)) if new_price > 0 else 1` — this guarantees quantity >= 1.
- No `if quantity == 0: return None` check added anywhere. `_compute_chunks` called with quantity already guaranteed >= 1 in all paths.
- Paper mode: `chunks = _compute_chunks(quantity)` at line 157. If chunks were somehow empty, `order_ids[0]` at line 171 would raise IndexError — but that's caught by the outer `except Exception` at line 337, which returns None. This would be a silent fill failure, but quantity >= 1 prevents it.
- Fee calc at entry: NO fee math happens during `place_order()` entry path. Fee calculation only runs in `close_position()` paper mode path, wrapped in `try/except` (lines 409–411). Cannot block entry.
- No new minimum contract count check introduced.

**No silent failure path from this fix. Entry-side: safe.**

---

## Fix 6 — Fee Math (`core/fee_calculator.py`, `scalper/order_manager.py`, etc.)

**VERDICT: CLEAN**

Audit findings on `fee_calculator.py`:
- `calculate_fee(quantity, price_cents)`: divides by 100 (safe), calls `min(yes_price, no_price)`. If `price_cents=0`: yes_price=0.0, no_price=1.0, min=0.0, fee=0.0 — no exception, returns 0.0. If `price_cents=100`: yes_price=1.0, no_price=0.0, min=0.0, fee=0.0 — no exception. `price_cents=None` would cause `None / 100.0` → TypeError, but all callers pass integer cents.
- In `close_position()` paper path (lines 409–412), fee calc is wrapped: `try: _fees_paid = _crf(...)  except Exception: _fees_paid = 0.0`. Cannot throw to caller.
- **No fee math runs during entry** (`place_order()`). Searched entire `place_order()` — no calls to `fee_calculator`, no `fees_paid` or `net_edge` computation, no edge threshold gate. Fee calculation is exit-only.
- `core/agent_tier_manager.py` is called at entry via `get_agent_tier()` (line 205), but that call is also wrapped in `try/except` (lines 204–208). Cannot block entry.

**No entry-side blockage from fee math. Exit path safely guarded. Clean.**

---

## Fix B — Per-position `max_hold_minutes` (`scalper/scalper_engine.py _check_time_exits()`)

**VERDICT: CLEAN**

Audit of `_check_time_exits()` lines 679–717:
```python
pos_max = getattr(position, "max_hold_minutes", None)
max_hold_minutes = pos_max if (pos_max is not None and pos_max > 0) else config_max_hold_minutes
max_hold_seconds = max_hold_minutes * 60
```
- `pos_max > 0` correctly gates zero. A position with `max_hold_minutes=0` falls back to `config_max_hold_minutes` (read from config, default `_DEFAULT_MAX_HOLD = 30`).
- `max_hold_seconds` is computed INSIDE the loop but correctly derived from `max_hold_minutes`, not hardcoded. No risk of 0 unless `config_max_hold_minutes` itself is 0 — which would require `max_hold_minutes: 0` in `syndicate_config.yaml` with the fallback also being 0.
- In `place_order()`, `max_hold_minutes = int(rule.get("max_hold_minutes", 60))` — default is 60, not 0.
- Per-position `max_hold_minutes` can only be 0 if TC explicitly writes `max_hold_minutes: 0` in the decision. The `> 0` guard prevents instant-exit in that case, falling back to config value.

**Logic is sound. No instant-exit regression. Clean.**

---

## Fix C — `SUM(COALESCE(net_pnl, pnl, 0))` in `warroom/app.py _load_agent_stats()`

**VERDICT: CLEAN**

Current code (line 227):
```sql
ROUND(SUM(COALESCE(net_pnl, pnl, 0)), 2) as total_pnl
```
Valid SQLite. `COALESCE` with 3 args is standard. This is a dashboard-only read query with no effect on trade execution.

---

## Fix D — Session P&L CT timezone filter in `warroom/app.py _get_fleet_intel()`

**VERDICT: CLEAN**

Code (lines 143–150):
```python
ct_today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")
cur.execute("""
    SELECT COALESCE(SUM(COALESCE(net_pnl, pnl)), 0)
    FROM syndicate_trades
    WHERE exit_time IS NOT NULL
      AND date(datetime(exit_time, '-5 hours')) = ?
""", (ct_today,))
```
- `ct_today` is a Python string variable bound as a parameter (not string interpolation) — no injection risk and no malformation.
- Hardcoded `-5 hours` offset is correct for CDT (UTC-5) in April.
- `_get_fleet_intel()` is wrapped in `try/except pass` — any error returns the default `result` dict with zeros. Dashboard-only, no execution impact.

---

## Fix 5 — End-to-End Verification Artifacts

**VERDICT: CLEAN — no harmful artifacts**

Triggers directory checked. Contents:
- `ace_signal.json`, `axiom_signal.json`, `cipher_signal.json`, `delta_signal.json`, `diamond_signal.json`, `ghost_signal.json` — live agent signal files (NOT test artifacts)
- `heartbeat_latest.json`, `opportunity_scan.json`, `strategic_scan.json` — permanent scan files (protected from Sweeper)
- 60+ `velocity_*.json` files — generated by ScanEngine velocity detection (all timestamped 14:25 UTC April 17)
- `phoenix_signal.json` — live agent signal

No leftover test files or `*_test.json` artifacts observed. Signal files are live (not stale). No harmful artifacts from Fix 5 verification.

---

## Fix 1 — `intelligence/wake_syndicate.ps1` TC Gate Signal Injection

**VERDICT: SUSPECT — not a code regression, but likely the scene of the crime**

The fix added the agent signal flow (Code path 2: `{name}_signal.json → {name}_decision.json`). This is the primary execution path.

**What the log proves:**
- April 17, 00:26 UTC: `[Gate] Poll thread started` — restart occurred
- April 17, 01:04:31 UTC: `[Sweeper] Purged 11 stale trigger files` + session P&L reset to $0.00 (another restart)
- After 01:04 UTC: Agents generate signals continuously (confirmed), `{name}_signal.json` files exist in triggers/, but **zero `[Gate] Agent decision:` log entries appear anywhere on April 17** despite 6+ agents submitting signals every scan cycle
- Signal files ARE being written (confirmed: `ace_signal.json` etc. exist), but no corresponding `_decision.json` files appear

**Root cause inference:** `wake_syndicate.ps1` is not running, OR the `claude` CLI it calls is failing silently. The PS1 is the bridge between signal files and decision files. Without it running, agent signals pile up → either expire → or get swept.

Note: The Sweeper at line 154 explicitly protects `*_signal.json` and `*_decision.json` from being swept. So signals are not being swept — they are simply not being picked up by a running PS1 process.

**This fix introduced a hard dependency on wake_syndicate.ps1 running continuously. If that process died or was never (re)started after the 01:04 restart, the entire execution path is severed.**

---

## Fix A — Backfill NULL fee rows (`scripts/backfill_fees.py`)

**VERDICT: CLEAN**

DB-only backfill script. No runtime execution impact.

---

## Python Syntax Checks

All four files compiled clean:
- `scalper/scalper_engine.py` — OK
- `scalper/order_manager.py` — OK  
- `core/outcome_reporter.py` — OK
- `warroom/app.py` — OK

---

## Log Edge/Threshold Check

No `edge`, `threshold`, `MIN_EDGE`, or `too small` log entries found for April 17. Edge values in agent signals are healthy (8.9%–65.28%). No edge-based entry blocking is occurring.

---

## Summary Table

| Fix | File(s) | Verdict | Notes |
|-----|---------|---------|-------|
| Fix 1 | `intelligence/wake_syndicate.ps1` | SUSPECT | PS1 not processing signals — zero decisions logged April 17 |
| Fix 2 | `scalper/order_manager.py` | CLEAN | Chunking correct; no quantity=0 path; no entry-side fee gate |
| Fix 3 | `agents/cipher.py` | CLEAN | Docs only |
| Fix 4 | `warroom/app.py`, `dashboard.html` | CLEAN | Dashboard read-only |
| Fix 5 | (verification) | CLEAN | No harmful artifacts in triggers/ |
| Fix 6 | `core/fee_calculator.py` + others | CLEAN | Fee math exit-only, all guarded |
| Fix A | `scripts/backfill_fees.py` | CLEAN | DB-only, no runtime impact |
| Fix B | `scalper/scalper_engine.py` | CLEAN | `pos_max > 0` guard correct |
| Fix C | `warroom/app.py` | CLEAN | Dashboard-only SQL fix |
| Fix D | `warroom/app.py` | CLEAN | Dashboard-only, parameterized query |

---

## Primary Conclusion

**None of Fixes 2–6 or A–D introduced a regression that blocks trade execution.**

The execution freeze has one cause: **the TC Gate pipeline is severed**. Agent signals are being written to `triggers/` but `wake_syndicate.ps1` is not consuming them (no `_decision.json` files produced, no `[Gate] Agent decision:` log entries on April 17). Without TC decisions, `_act_on_decision()` never fires, and `place_order()` is never called.

**Action required:** Verify wake_syndicate.ps1 is running. Check if the `claude` CLI is accessible and authenticated. Restart the PS1 gate watcher if it died.
