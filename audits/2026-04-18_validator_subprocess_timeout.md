# Validator: Subprocess timeout (SAGE 60s WaitOne)
**Date:** 2026-04-18  
**Gate PID:** 26776 (stdout redirected to wake_syndicate.log)

## Setup

```python
# 1. Backup original
shutil.copy2("tools/get_sage_briefing.py", "tools/get_sage_briefing.py.bak")

# 2. Replace with 120s hang
with open("tools/get_sage_briefing.py", "w") as f:
    f.write("import time\ntime.sleep(120)\nprint('{}')\n")

# 3. Write AXIOM signal to trigger SAGE call
sig = {
    "agent_name": "AXIOM",
    "signal": {"ticker": "KXETHD-26APR1917-T2349.99", "entry_price": 0.20, ...},
    "expires_at": "...",
}
# Written to triggers/axiom_signal.json
```

## Gate Log Evidence

```
[Syndicate Gate] Agent signal: axiom_signal.json | agent=AXIOM
WARNING: [Syndicate Gate] SAGE timeout after 60s for AXIOM -- skipping
[Syndicate Gate] Invoking TC for AXIOM (max 120s)...
[Syndicate Gate] Decision written: axiom_decision.json
[Syndicate Gate] Agent flow done for AXIOM.
```

## Result: PASS

- SAGE hung at sleep(120)
- Gate WaitOne(60000) fired at exactly 60s with WARNING log
- Gate continued to TC call normally (not blocked/crashed)
- Decision was written — gate was not stuck

## Cleanup

Original `get_sage_briefing.py` restored from `.bak`. Backup deleted.

## Notes
- Same pattern applies to ECHO (WaitOne(60000)) and TC itself (WaitOne(120000))
- This confirms the runspace + AsyncWaitHandle.WaitOne pattern works correctly for subprocess gating
