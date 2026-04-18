# Syndicate Bug Fix Validation — 2026-04-17

Auditor: Claude Code  
DB: `syndicate/logs/syndicate_trades.db`  
Files checked: `scalper/scalper_engine.py`, `warroom/app.py`

---

## FIX A — NULL Fees

**PASS**

Q1 — Rows with NULL fees_paid OR net_pnl:
```
0 rows returned
```

Q2 — Closed trade count / NULL fees count:
```
Total closed trades: 103 | NULL fees_paid: 0
```

All 103 closed trades have fees_paid populated. No NULLs remain.

---

## FIX B — DELTA Hold-Time (per-position override)

**PASS**

Code in `_check_time_exits()` (lines ~691-698):
```python
pos_max = getattr(position, "max_hold_minutes", None)
max_hold_minutes = pos_max if (pos_max is not None and pos_max > 0) else config_max_hold_minutes
max_hold_seconds = max_hold_minutes * 60
```

Per-position `max_hold_minutes` is read inside the loop and overrides the config value when set and non-zero. Global config is correctly used only as fallback. No live DELTA closes yet to verify via DB — code inspection sufficient per spec.

---

## FIX C — Leaderboard COALESCE Bug

**PASS**

`_load_agent_stats()` SQL (line 227):
```sql
ROUND(SUM(COALESCE(net_pnl, pnl, 0)), 2) as total_pnl
```

Pattern is `SUM(COALESCE(...))` — inner COALESCE, outer SUM. Bug pattern `COALESCE(SUM(...), SUM(...))` is NOT present.

Ground-truth net P&L per agent (closed trades only):
```
AXIOM    32 trades  net=$54.58   gross=$62.12   fees=$7.53
SHADOW    2 trades  net=$4.23    gross=$4.48    fees=$0.25
DIAMOND   1 trade   net=-$0.67   gross=$0.00    fees=$0.67
GHOST     3 trades  net=-$0.80   gross=-$0.38   fees=$0.42
ACE       6 trades  net=-$3.32   gross=-$1.33   fees=$1.99
PHOENIX   3 trades  net=-$6.04   gross=-$4.41   fees=$1.63
DELTA    32 trades  net=-$7.70   gross=-$0.07   fees=$7.63
CIPHER   24 trades  net=-$16.36  gross=-$7.91   fees=$8.46
```

CIPHER shows **-$16.36** (not +$2.56). Bug is fixed — fees are being correctly deducted.

---

## FIX D — Session P&L Filter

**PASS**

`_get_fleet_intel()` session_pnl query:
```sql
SELECT COALESCE(SUM(COALESCE(net_pnl, pnl)), 0)
FROM syndicate_trades
WHERE exit_time IS NOT NULL
  AND date(datetime(exit_time, '-5 hours')) = ?
```

Bound parameter is `ct_today = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")`. Filters to today's CT date only — does NOT aggregate all-time.

Import confirmed: `from datetime import datetime, timezone, timedelta` (line 11).

Session query result for today (2026-04-16 CT):
```
Trades today: 0 | Session P&L: $0.00
```

No trades have closed today (CT), so dashboard correctly shows $0.00 — not the all-time total.

---

## Summary

| Fix | Description | Result |
|-----|-------------|--------|
| A | NULL fees/net_pnl | PASS |
| B | DELTA per-position hold-time | PASS |
| C | Leaderboard COALESCE ordering | PASS |
| D | Session P&L date filter | PASS |

**All four fixes validated. No remaining issues found.**
