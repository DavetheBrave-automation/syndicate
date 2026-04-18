# Validator: DELTA max_hold_minutes per-agent defaults
**Date:** 2026-04-18  
**Fix:** `main.py` `_act_on_decision` — added `_AGENT_MAX_HOLD_DEFAULTS = {"DELTA": 240}` so DELTA defaults to 240-min hold when TC decision omits the field.

## Test Script

```python
_AGENT_MAX_HOLD_DEFAULTS = {"DELTA": 240}

def compute_max_hold(agent_name, decision, is_htsr):
    _default_max_hold = _AGENT_MAX_HOLD_DEFAULTS.get(agent_name, 60)
    return 4320 if is_htsr else int(decision.get("max_hold_minutes", _default_max_hold))

def get_is_htsr(agent_name, decision):
    return (agent_name == "AXIOM") or bool(decision.get("hold_to_settlement", False))
```

## Test Cases and Results

| Case | Agent | Decision | Expected | Actual | Result |
|------|-------|----------|----------|--------|--------|
| DELTA missing field | DELTA | {} | 240 | 240 | PASS |
| DELTA explicit 240 | DELTA | {max_hold_minutes: 240} | 240 | 240 | PASS |
| DELTA explicit 60 | DELTA | {max_hold_minutes: 60} | 60 | 60 | PASS |
| AXIOM (HTSR) | AXIOM | {} | 4320 | 4320 | PASS |
| AXIOM explicit 60 (HTSR override) | AXIOM | {max_hold_minutes: 60} | 4320 | 4320 | PASS |
| CIPHER missing | CIPHER | {} | 60 | 60 | PASS |
| GHOST missing | GHOST | {} | 60 | 60 | PASS |
| ACE explicit 120 | ACE | {max_hold_minutes: 120} | 120 | 120 | PASS |

## Result: ALL PASS

## Notes
- DELTA gets 240 when TC omits field (key fix for Fix B regression)
- TC can still override DOWN to 60 via explicit `max_hold_minutes: 60` in decision
- AXIOM always gets 4320 (HTSR overrides everything) regardless of decision value
- All other agents default to 60 when field absent
- Config `scalper.max_hold_minutes: 60` unchanged — this is the global scalper default, not DELTA-specific
