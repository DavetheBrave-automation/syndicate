# Bench Display Audit — 2026-04-19

**Issue:** Promotion Watch shows CIPHER and DELTA. Both are benched. Dashboard is not reading bench state from the right source.

---

## Two Bench Systems (Out of Sync)

The engine and dashboard track bench state in different places:

### System A — ScanEngine (what controls actual trading)

`syndicate_config.yaml` → `active_agents`:
```yaml
active_agents:
  - AXIOM
  - SAGE
  - ECHO
```

If an agent is NOT in this list, it never sweeps markets. CIPHER and DELTA are absent → they don't trade. This is the authoritative bench source.

### System B — Dashboard Roster (what the dashboard reads)

`memory/{agent}.json` → `benched` boolean:
```json
// CIPHER.json
{ "benched": false, ... }

// DELTA.json
{ "benched": false, ... }   ← confirmed
```

Both files say `benched: false`. So the dashboard shows them as active even though the ScanEngine isn't running them.

**Root cause: the two systems were never synchronized when CIPHER/DELTA were benched via config.**

---

## Promotion Watch — Where It Gets Data

`app.py` → `_get_fleet_intel()` → lines 178–198:

```python
from core.agent_tier_manager import get_all_tiers, get_promotion_progress

for a in get_all_tiers():          # ← queries DB, no bench check
    prog = get_promotion_progress(a["agent"])
    if prog and prog["trades_needed"] <= 10 and prog["wins_needed"] <= 6:
        result["promotion_watch"].append(...)
```

`get_all_tiers()` pulls every agent that has ANY trades in DB. No active_agents check, no memory benched check. This is why benched agents appear.

---

## Agent Roster Panel — Where It Gets Data

`app.py` lines 319–333:
```python
for name in _KNOWN_AGENTS:
    mem   = mems.get(name, {})     # ← reads memory/{name}.json
    stats = agent_stats.get(name, ...)
    roster.append({
        "benched": mem.get("benched", False),   # ← reads memory file
        ...
    })
```

Reads from memory file. CIPHER and DELTA both have `benched: false` in memory → show as active. The config `active_agents` list is never consulted here.

---

## Secondary Bug — `get_promotion_progress` Understates wins_needed for DELTA

DELTA: 39 trades, 10 wins = 25.6% WR. Tier: PROBATION.

```python
wins_for_next  = int(min_trades * min_wr)   # int(20 * 0.50) = 10
current_wins   = int(wr * trades)           # int(0.256 * 39) = 10
wins_needed    = max(0, wins_for_next - current_wins)  # max(0, 10-10) = 0
trades_needed  = max(0, min_trades - trades)           # max(0, 20-39) = 0
```

Returns `trades_needed=0, wins_needed=0` → threshold check passes (0 ≤ 10, 0 ≤ 6) → DELTA appears in Promotion Watch.

But DELTA actually needs **~10 more wins** before its win rate (currently 25.6%) can reach the 50% qualified threshold. The formula computes wins needed against `min_trades` (20), not against DELTA's actual trade count (39). Any agent with trades > min_trades and poor win rate hits this bug.

Correct formula should be:
```python
wins_needed = max(0, ceil(current_trades * min_wr) - current_wins)
# ceil(39 * 0.50) - 10 = 20 - 10 = 10  ← correct
```

---

## Summary Table

| Panel | Data Source | Bench-Aware? | CIPHER shows? | DELTA shows? |
|-------|-------------|:------------:|:-------------:|:------------:|
| Agent Roster | `memory/{name}.json` → `benched` | ❌ wrong source | ✅ as active | ✅ as active |
| Promotion Watch | `agent_tier_manager.get_all_tiers()` | ❌ none | ✅ incorrectly | ✅ via bug |
| Tier Distribution | `agent_tier_manager.get_all_tiers()` | ❌ none | ✅ as QUALIFIED | ✅ as PROBATION |

---

## Proposed Fix (pending David approval)

**Option A — Fix the memory files (minimal change):**
Set `"benched": true` in `memory/CIPHER.json` and `memory/DELTA.json`.
- Dashboard roster then shows them as benched correctly.
- Promotion Watch still needs a bench check added.
- Fragile: next time an agent is benched via config, memory file won't be auto-updated.

**Option B — Single source of truth (recommended):**
1. In `app.py`, load `active_agents` from config at startup.
2. For roster: mark agent as benched if `name not in active_agents`.
3. For Promotion Watch: skip any agent not in `active_agents`.
4. Fix `get_promotion_progress` `wins_needed` calculation.

Roster display behavior options (your call):
- **Hide entirely** — cleanest; bench = irrelevant until unbench
- **Grey out with "BENCHED" badge** — preserves historical stats visibility
- **Exclude from Promotion Watch only** — they can't promote if not trading

**Do NOT implement without David's approval.**
