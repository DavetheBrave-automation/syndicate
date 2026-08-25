# Agent Bench — Implementation Record
**Date:** 2026-04-18  
**Time:** ~22:10 UTC  
**Status:** Implemented, pending engine restart to activate

---

## Decision: Config-key filter in `scan_engine.py` `__init__`

Two options were evaluated:

| Option | Approach | Verdict |
|--------|----------|---------|
| A | `active_agents` top-level list in config → filter in `__init__` | **CHOSEN** |
| B | Each agent's `evaluate()` checks an `enabled` flag and returns `None` | Rejected — requires touching 13 agent files |

**Why A:** Single config key, single enforcement point, zero agent-file changes. Filter runs once at startup — benched agents never enter `self._agents` and therefore never receive `evaluate()` calls. CPU cost = zero at runtime.

Note: `validation_phase.benched_agents` already existed in config but enforcement was stripped on 2026-04-14. This implementation uses a new top-level `active_agents` key to avoid confusion with that legacy field.

---

## Files Changed

### 1. `syndicate_config.yaml` — top of file

Added before `syndicate:` block:

```yaml
# Active agents bench — only listed agents are registered in ScanEngine.
# To re-enable an agent: add its name here + restart engine.
# To disable bench (all 16 active): remove this key entirely + restart engine.
active_agents:
  - AXIOM
  - SAGE
  - ECHO
```

### 2. `core/scan_engine.py` — `ScanEngine.__init__()`, after agent list

Added after `self._agents = [...]` (line ~269):

```python
# ── Active agents bench filter ─────────────────────────────────────
# Reads active_agents list from syndicate_config.yaml.
# If key absent → all 16 active (no change). If present → only listed
# agents receive evaluate() calls. Reversible: add/remove names + restart.
_bench_cfg = _load_config()
_active_agents = _bench_cfg.get("active_agents", None)
if _active_agents is not None:
    _before_names = [a.name for a in self._agents]
    self._agents = [a for a in self._agents if a.name in _active_agents]
    _benched = [n for n in _before_names if n not in _active_agents]
    if _benched:
        logger.info("[ScanEngine] Bench active — benched: %s", _benched)
```

---

## Agents Benched (13)

| Agent | Prior Status | Reason for Bench |
|-------|-------------|-----------------|
| ACE | PARK | 16.7% WR, small sample |
| CIPHER | FIX | -$47.87 net, 0.20x win/loss ratio, structural bug |
| DELTA | FIX | -$30.51 net, Fix B unconfirmed in live runtime |
| DIAMOND | FIX | -$11.09 net, TC conversion blocker |
| PHOENIX | RETIRE | 0% WR, 3 trades |
| GHOST | PARK | ATP-dependent, series not in scan |
| BLITZ | PARK | Velocity threshold never triggered in BTC range |
| ENDGAME | PARK | Requires final-set ATP match, none active |
| SHADOW | PARK | Depends on primary agents, which are benched |
| ORACLE | PARK | KXFED at 99.5¢, above ghost-zone filter |
| TIDE | PARK | No activity, market conditions |
| MIRROR | PARK | No activity, market conditions |
| OIL | PARK | KXWTIW/KXWTIMAX not in current scan |

## Agents Active (3)

| Agent | Role | Why Active |
|-------|------|-----------|
| AXIOM | Trading — KEEP | Only profitable agent (+$25.70 net, 51.5% WR) |
| SAGE | Infrastructure | Briefing triggers / pattern stats — not a trading agent |
| ECHO | Infrastructure | Echo block writes — not a trading agent |

---

## How to Re-Enable an Agent

1. Open `syndicate_config.yaml`
2. Add the agent name to `active_agents` list
3. Restart engine

Example — re-enabling DELTA for Fix B runtime validation:
```yaml
active_agents:
  - AXIOM
  - DELTA    # ← add this line
  - SAGE
  - ECHO
```

## How to Disable Bench Entirely (all 16 agents)

Remove the `active_agents` key from `syndicate_config.yaml` entirely (or comment it out). The filter code treats `active_agents` absent as "no filter." Restart engine.

---

## Activation Status

**Pending restart.** Both files have been modified. The running engine (PID 22096) still has all 16 agents loaded from its prior `__init__`. Changes take effect on next engine start.

**Expected log output on restart:**
```
[ScanEngine] Bench active — benched: ['ACE', 'DIAMOND', 'PHOENIX', 'BLITZ', 'GHOST', 'ENDGAME', 'SHADOW', 'ORACLE', 'TIDE', 'CIPHER', 'DELTA', 'MIRROR', 'OIL']
[ScanEngine] Agents loaded: ['AXIOM', 'SAGE', 'ECHO']
```

**Validation checklist (post-restart):**
- [ ] Log shows `Bench active — benched: [13 agents]`
- [ ] Log shows `Agents loaded: ['AXIOM', 'SAGE', 'ECHO']`
- [ ] 30-minute observation: zero signals from benched agents
- [ ] Dashboard continues rendering (no all-agents dependency)

---

## Rollback

Git diff shows two files changed. To roll back:
```bash
git checkout -- syndicate_config.yaml core/scan_engine.py
```
Then restart engine.
