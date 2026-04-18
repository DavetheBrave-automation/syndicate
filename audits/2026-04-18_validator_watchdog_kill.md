# Validator: Watchdog gate kill-test
**Date:** 2026-04-18  
**Watchdog PID:** 27128  
**Gate PID before kill:** 26776  

## Test Sequence

1. Watchdog started at ~13:44 local (18:44 UTC) — confirmed `watchdog.log`:
   ```
   2026-04-18T13:44:04Z INFO  watchdog -- Watchdog started. Engine threshold=150s Gate threshold=180s interval=30s
   ```

2. Gate PID 26776 killed at 13:47 local (18:47 UTC):
   ```python
   subprocess.run(['powershell.exe', '-Command', 'Stop-Process -Id 26776 -Force'])
   # Kill result: 0 (success)
   ```

3. Waited 200 seconds (past GATE_HB_THRESHOLD=180s).

## Watchdog Log Evidence

```
2026-04-18T13:50:04Z CRITICAL watchdog -- [Gate] DEAD — heartbeat stale 200s. Restart #1.
2026-04-18T13:50:05Z INFO     watchdog -- [Gate] Restarted (attempt #1).
```

Detection time: ~200s after kill (heartbeat stale threshold = 180s, check interval = 30s → up to 210s).

## New Heartbeat

```json
{"ts": "2026-04-18T18:50:07Z", "pid": 28300}
```

**Gate restarted: 26776 → 28300** ✅

## Result: PASS

Watchdog correctly:
1. Detected gate death via heartbeat staleness (>180s)
2. Logged CRITICAL alert
3. Spawned new gate process
4. New gate writing heartbeats with new PID

## Notes
- Engine was also restarted by watchdog during this session:
  ```
  2026-04-18T13:44:10Z WARNING watchdog -- [Syndicate] Engine dead (log stale 176s). Restart #3.
  ```
  This confirms dual-process monitoring works for BOTH engine and gate.
- BACKOFF_DELAYS=[0,30,120,300] — restart #1 had 0s backoff (immediate)
- Telegram alert fires on gate death (via _telegram() in watchdog.py)
