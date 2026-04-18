# Syndicate Data Reconciliation Audit
**Date:** 2026-04-17  
**Auditor:** Claude Code (automated)  
**DB Path:** `C:\Users\djnec\CommandCenter\syndicate\logs\syndicate_trades.db`  
**SQLite `now()`:** 2026-04-17 05:15 UTC  
**Scope:** Read-only. No code changes.

---

## 1. Schema Dump

```sql
CREATE TABLE syndicate_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    entry_price    INTEGER NOT NULL,
    exit_price     REAL,
    quantity       INTEGER NOT NULL,
    pnl            REAL,
    hold_seconds   REAL,
    exit_reason    TEXT,
    rule_id        TEXT,
    agent_name     TEXT,
    contract_class TEXT,
    entry_time     TEXT    NOT NULL,
    exit_time      TEXT    NOT NULL,
    order_id       TEXT,
    fees_paid      REAL,     -- added Fix 6
    net_pnl        REAL      -- added Fix 6
);

CREATE TABLE syndicate_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

**Column mapping confirmed:**
- Table: `syndicate_trades` (not `closed_trades`)
- Closed filter: `exit_time IS NOT NULL` (not `closed_at`)
- Gross P&L column: `pnl` (not `gross_pnl`)
- Net P&L column: `net_pnl` (added, present)
- Fees column: `fees_paid` (added, present)

**syndicate_meta contents:**
```
data_valid_from            = 2026-04-15T20:47:05.882462+00:00
data_valid_from_trade_id   = 33
```
Records with id <= 33 are pre-fix data (before fee accounting was corrected).

**NULL data rows (2 records — partial fix coverage):**
| id  | agent_name | pnl       | fees_paid | net_pnl |
|-----|-----------|-----------|-----------|---------|
| 102 | CIPHER    | -14.355   | NULL      | NULL    |
| 103 | DELTA     | -0.045    | NULL      | NULL    |

---

## 2. Query Results

### Query 1 — Per-agent net P&L totals (all closed trades)

```sql
SELECT agent_name, COUNT(*) as trades,
  SUM(COALESCE(net_pnl, pnl)) as total_net,
  SUM(pnl) as total_gross,
  SUM(COALESCE(fees_paid, 0)) as total_fees,
  SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as net_win_rate
FROM syndicate_trades WHERE exit_time IS NOT NULL
GROUP BY agent_name ORDER BY total_net DESC;
```

| agent_name | trades | total_net  | total_gross | total_fees | net_win_rate |
|-----------|--------|-----------|------------|-----------|-------------|
| AXIOM     | 32     | $54.58    | $62.12     | $7.53     | 53.1%       |
| SHADOW    | 2      | $4.23     | $4.48      | $0.25     | 100.0%      |
| DIAMOND   | 1      | -$0.67    | $0.00      | $0.67     | 0.0%        |
| GHOST     | 3      | -$0.80    | -$0.38     | $0.42     | 33.3%       |
| ACE       | 6      | -$3.32    | -$1.33     | $1.99     | 16.7%       |
| PHOENIX   | 3      | -$6.04    | -$4.41     | $1.63     | 0.0%        |
| DELTA     | 32     | -$7.55    | -$0.07     | $7.48     | 25.0%       |
| CIPHER    | 24     | -$11.79   | -$7.91     | $3.89     | 62.5%       |
| **TOTAL** | **103**| **$28.65**| **$52.50** | **$23.85**| —           |

**NOTE on CIPHER:** Row-level `SUM(COALESCE(net_pnl, pnl))` = **-$11.79** but the dashboard's `_load_agent_stats` uses aggregate-level `COALESCE(SUM(net_pnl), SUM(pnl))` = **+$2.56**. This is a logic discrepancy — see Section 4.

---

### Query 2 — Session P&L (today, DATE = 2026-04-17 UTC)

```sql
SELECT COUNT(*) as trades_today,
  SUM(COALESCE(net_pnl, pnl)) as session_net,
  SUM(COALESCE(fees_paid, 0)) as session_fees
FROM syndicate_trades
WHERE exit_time IS NOT NULL AND DATE(exit_time) = DATE('now');
```

| trades_today | session_net | session_fees |
|-------------|------------|-------------|
| 7           | -$17.31    | $1.39       |

---

### Query 3 — 24-hour rolling (from 2026-04-17 05:15 UTC back 24h)

```sql
SELECT COUNT(*) as trades_24h,
  SUM(COALESCE(net_pnl, pnl)) as pnl_24h,
  SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END)*1.0/COUNT(*) as win_rate_24h
FROM syndicate_trades
WHERE exit_time IS NOT NULL AND exit_time >= datetime('now', '-24 hours');
```

| trades_24h | pnl_24h  | win_rate_24h |
|-----------|---------|-------------|
| 59        | -$25.42 | 39.0%       |

---

### Query 4 — Double-fee detection (CRITICAL)

```sql
SELECT id, ticker, agent_name, pnl, fees_paid, net_pnl,
  (pnl - fees_paid) as computed_net,
  (net_pnl - (pnl - fees_paid)) as discrepancy
FROM syndicate_trades
WHERE exit_time IS NOT NULL AND net_pnl IS NOT NULL AND pnl IS NOT NULL
  AND ABS(net_pnl - (pnl - fees_paid)) > 0.01
LIMIT 20;
```

**Discrepancy rows: 0**

Coverage stats:
- Rows with `net_pnl` populated: 101 of 103 closed trades
- Rows with `fees_paid` populated: 101 of 103 closed trades
- Rows missing both (ids 102, 103): 2

**Double-fee check result: CLEAN** (among rows where both columns are populated)

*Note: ids 102 and 103 have NULL fees_paid and NULL net_pnl — they fall back to gross pnl. These 2 rows are excluded from the double-fee check but are flagged separately below.*

---

### Query 5 — DELTA max-hold exits (last 48h)

```sql
SELECT id, ticker, entry_price, exit_price,
  entry_time, exit_time,
  (julianday(exit_time) - julianday(entry_time)) * 24 * 60 as hold_minutes,
  exit_reason
FROM syndicate_trades
WHERE agent_name = 'DELTA' AND exit_reason LIKE '%max hold%'
  AND exit_time >= datetime('now', '-48 hours')
ORDER BY exit_time DESC LIMIT 20;
```

20 rows returned (LIMIT hit). Sample (most recent 5):

| id  | ticker                          | hold_min | exit_reason                                    |
|-----|---------------------------------|----------|------------------------------------------------|
| 103 | KXBTCD-26APR1717-T74499.99      | 60.47    | max hold time exceeded: held=60.4m >= max=60m  |
| 99  | KXBTCD-26APR1717-T75499.99      | 60.08    | max hold time exceeded: held=60.1m >= max=60m  |
| 96  | KXBTCD-26APR1717-T74999.99      | 60.45    | max hold time exceeded: held=60.4m >= max=60m  |
| 94  | KXBTCD-26APR1717-T74499.99      | 60.22    | max hold time exceeded: held=60.2m >= max=60m  |
| 79  | KXBTCD-26APR1617-T74749.99      | 60.38    | max hold time exceeded: held=60.3m >= max=60m  |

All hold_minutes in range 60.0–60.5 min. Max-hold exits are triggering correctly at the 60-minute boundary.

All-time DELTA exit reason breakdown:
- max hold exits: 21 of 32 trades (65.6%) — dominant exit mode
- Target hit: 6 trades (18.8%)
- Stop loss: 5 trades (15.6%)

**DELTA hold-time check: WORKING**

---

### Query 6 — Exit reason breakdown (last 24h)

26 distinct exit reason groups in last 24h. Full results:

| exit_reason                                    | count | total_pnl  | avg_pnl   |
|-----------------------------------------------|-------|-----------|----------|
| max hold time exceeded: held=60.4m >= max=60m  | 6     | -$1.90    | -$0.32   |
| max hold time exceeded: held=60.3m >= max=60m  | 6     | -$2.25    | -$0.37   |
| Target hit: +21%                               | 6     | +$2.19    | +$0.37   |
| max hold time exceeded: held=60.2m >= max=60m  | 5     | -$1.40    | -$0.28   |
| max hold time exceeded: held=60.1m >= max=60m  | 5     | -$1.41    | -$0.28   |
| Target hit: +23%                               | 5     | +$3.10    | +$0.62   |
| max hold time exceeded: held=60.5m >= max=60m  | 2     | -$0.03    | -$0.02   |
| max hold time exceeded: held=60.0m >= max=60m  | 2     | -$0.53    | -$0.26   |
| Target hit: +24%                               | 2     | +$0.66    | +$0.33   |
| Target hit: +22%                               | 2     | +$0.93    | +$0.46   |
| Stop loss: -46%                                | 2     | -$3.39    | -$1.69   |
| Settlement protection: losing near close       | 2     | -$14.81   | -$7.41   |
| Time stop — underwater -2% with <1hr to settle | 1     | -$0.44    | -$0.44   |
| Target hit: +40%                               | 1     | +$0.67    | +$0.67   |
| Target hit: +39%                               | 1     | +$1.58    | +$1.58   |
| Target hit: +30%                               | 1     | +$1.19    | +$1.19   |
| Target hit: +27%                               | 1     | +$0.67    | +$0.67   |
| Target hit: +20%                               | 1     | +$0.23    | +$0.23   |
| Stop loss: -50%                                | 1     | -$1.47    | -$1.47   |
| Stop loss: -39%                                | 1     | -$1.30    | -$1.30   |
| Stop loss: -37%                                | 1     | -$1.63    | -$1.63   |
| Stop loss: -36%                                | 1     | -$0.62    | -$0.62   |
| Stop loss: -34%                                | 1     | -$1.26    | -$1.26   |
| Stop loss: -33%                                | 1     | -$1.99    | -$1.99   |
| Settlement risk — other side at 94%, cutting   | 1     | -$2.45    | -$2.45   |
| Profit target — our side at 86%               | 1     | +$0.21    | +$0.21   |

**Key observation:** "Settlement protection: losing near close" = 2 trades, -$14.81 combined. This is the single largest P&L drain in the 24h window (avg -$7.41 per trade). All-time this reason accounts for 9 trades and -$21.86.

---

### Query 6b — All-time exit reason breakdown (top items)

| exit_reason                                    | count | total_pnl  |
|-----------------------------------------------|-------|-----------|
| Settlement protection: losing near close       | 9     | -$21.86   |
| Target hit: +23%                               | 7     | +$4.37    |
| Win locked — our side at 95%                   | 3     | +$50.75   |
| Target hit: +68%                               | 3     | +$6.35    |
| max hold time exceeded: held=60.4m >= max=60m  | 6     | -$1.90    |
| max hold time exceeded: held=60.3m >= max=60m  | 6     | -$2.25    |

---

## 3. Dashboard Code — fleet_intel Queries (from warroom/app.py)

### session_pnl (lines 138–141)
```python
cur.execute("SELECT COUNT(*), COALESCE(SUM(COALESCE(net_pnl, pnl)),0) 
             FROM syndicate_trades WHERE exit_time IS NOT NULL")
result["closed_trades"] = int(r[0] or 0)
result["session_pnl"]   = round(float(r[1] or 0), 2)
```
**This is ALL-TIME net P&L, not today's session.** The label `session_pnl` is misleading — it sums every closed trade ever.

### pnl_24h (lines 143–155)
```python
cur.execute("""
    SELECT COUNT(*),
           SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END),
           COALESCE(SUM(COALESCE(net_pnl, pnl)), 0)
    FROM syndicate_trades
    WHERE exit_time IS NOT NULL
      AND exit_time >= datetime('now', '-24 hours')
""")
result["trades_24h"]  = t24
result["winrate_24h"] = round(int(r[1] or 0) / t24 * 100) if t24 else 0
result["pnl_24h"]     = round(float(r[2] or 0), 2)
```

### leaderboard pnl (_load_agent_stats, lines 214–236)
```python
ROUND(COALESCE(SUM(net_pnl), SUM(pnl), 0), 2) as total_pnl
```
**This is aggregate-level COALESCE** — if any net_pnl rows exist for an agent, SUM(net_pnl) will be non-NULL (even if some rows have NULL net_pnl), so it returns SUM of only non-NULL net_pnl rows. This silently drops fallback pnl for rows where net_pnl IS NULL.

### total_pnl top-right stat (lines 350–354)
```python
total_pnl = round(sum(
    float(t.get("net_pnl") if t.get("net_pnl") is not None else t.get("pnl") or 0)
    for t in trades if int(t.get("id") or 0) > valid_from_id
), 2)
```
Uses Python-level row COALESCE on last 200 trades filtered by `id > 33`.

---

## 4. Comparison Table — DB vs Dashboard

### A. Session P&L

| Item | DB (Query 2, today) | Dashboard `fleet_intel.session_pnl` | Match? |
|------|---------------------|-------------------------------------|--------|
| Label meaning | Today's closed trades | ALL-TIME closed trades | — |
| Trade count | 7 | 103 | MISMATCH (by design) |
| Net P&L value | -$17.31 | +$28.65 | MISMATCH |

**Assessment:** MISMATCH — but this is a **naming issue, not a data corruption issue**. The dashboard variable is called `session_pnl` but computes all-time P&L. If the UI displays it as "session P&L" to the user, this is misleading.

---

### B. 24h P&L

| Item | DB (Query 3) | Dashboard `fleet_intel.pnl_24h` | Match? |
|------|-------------|----------------------------------|--------|
| Trade count | 59 | 59 | MATCH |
| Net P&L | -$25.42 | -$25.42 | MATCH |
| Win rate | 39.0% | 39% | MATCH |

---

### C. Per-agent Leaderboard (all closed trades)

Comparing Query 1 `SUM(COALESCE(net_pnl, pnl))` vs dashboard `COALESCE(SUM(net_pnl), SUM(pnl))`:

| Agent  | DB Query 1 (correct) | Dashboard Leaderboard | Match? |
|--------|---------------------|-----------------------|--------|
| AXIOM  | +$54.58             | +$54.58               | MATCH  |
| SHADOW | +$4.23              | +$4.23                | MATCH  |
| DIAMOND| -$0.67              | -$0.67                | MATCH  |
| GHOST  | -$0.80              | -$0.80                | MATCH  |
| ACE    | -$3.32              | -$3.32                | MATCH  |
| PHOENIX| -$6.04              | -$6.04                | MATCH  |
| DELTA  | -$7.55              | -$7.50                | 🚨 MISMATCH: $0.045 gap |
| CIPHER | -$11.79             | +$2.56                | 🚨 MISMATCH: $14.35 gap |

**Root cause of mismatches:**

**CIPHER:** Trade id=102 has `pnl=-14.355`, `fees_paid=NULL`, `net_pnl=NULL`. In Query 1 (`SUM(COALESCE(net_pnl,pnl))`), this row contributes **-$14.355** (falls back to pnl). In the dashboard's `_load_agent_stats` (`COALESCE(SUM(net_pnl), SUM(pnl))`), `SUM(net_pnl)` is non-NULL (23 other rows have net_pnl), so it returns `SUM(net_pnl)` = **+$2.56**, silently **dropping the -$14.355 contribution from id=102**.

**DELTA:** Trade id=103 has `pnl=-0.045`, `fees_paid=NULL`, `net_pnl=NULL`. Same pattern — `SUM(net_pnl)` returns -$7.50 (31 rows), missing the -$0.045 pnl fallback.

The dashboard leaderboard **overstates CIPHER by +$14.355** and **overstates DELTA by +$0.045** because of aggregate-level vs row-level COALESCE difference when NULL rows exist.

---

### D. Total P&L (top-right dashboard stat, `id > 33`, last 200 trades)

| Item | DB (all closed, id > 33) | Dashboard `total_pnl` | Match? |
|------|--------------------------|----------------------|--------|
| Net P&L | +$16.61* | +$16.61 | MATCH |

*Computed identically: Python row-level COALESCE on trades with id > 33. CIPHER id=102 and DELTA id=103 are both > 33, so they use fallback pnl correctly here. This stat is accurate.

---

## 5. Double-Fee Check Result

**CLEAN** — No rows where `|net_pnl - (pnl - fees_paid)| > $0.01` among the 101 rows that have both columns populated. Fee accounting is consistent.

🚨 **However:** 2 rows (id=102 CIPHER, id=103 DELTA) have NULL fees_paid and NULL net_pnl. These are the most recent significant trade (id=102 pnl=-$14.36) and the most recent DELTA trade (id=103). These were likely closed while the fee-recording code had a failure. They are recorded with gross pnl only.

---

## 6. DELTA Hold-Time Check

**WORKING** — 20+ max-hold exits in the last 48h, all triggering correctly at 60.0–60.5 minutes (consistent with the "held=60.Xm >= max=60m" log format). Hold time calculation via julianday arithmetic is accurate. No trades held beyond 61 minutes.

DELTA is triggering max-hold on 65.6% of its trades (21/32), which is the primary driver of its net loss (-$7.55 all-time) — the hold-time mechanic works but the strategy leaks on max-hold exits.

---

## 7. Summary

| Check | Result |
|-------|--------|
| Schema introspected | PASS — table is `syndicate_trades`, columns confirmed |
| Double-fee detection | CLEAN (101/103 rows checked) |
| DELTA hold-time | WORKING |
| 24h P&L match | MATCH |
| Session P&L match | MISMATCH (naming issue — dashboard shows all-time as "session") |
| CIPHER leaderboard | 🚨 MISMATCH: dashboard shows +$2.56, DB truth is -$11.79 (delta = $14.35) |
| DELTA leaderboard | 🚨 MISMATCH: dashboard shows -$7.50, DB truth is -$7.55 (delta = $0.05) |
| Total P&L top-right | MATCH ($16.61) |
| NULL fee rows | 🚨 2 rows (id=102 CIPHER, id=103 DELTA) with no fees_paid/net_pnl |

**Overall: FAIL**

### Issues requiring action

1. **🚨 CRITICAL — CIPHER leaderboard wrong by $14.35:** Bug in `_load_agent_stats` in `warroom/app.py` line 218. `COALESCE(SUM(net_pnl), SUM(pnl), 0)` must be changed to `SUM(COALESCE(net_pnl, pnl))` to correctly handle rows where net_pnl is NULL. Currently CIPHER shows +$2.56 when the correct figure is -$11.79.

2. **🚨 MODERATE — NULL fee rows for ids 102 and 103:** Most recent CIPHER trade (-$14.36 gross) and most recent DELTA trade (-$0.045 gross) have no fees_paid or net_pnl recorded. Fee backfill needed for these rows, or investigation of why the fee-writing code silently skipped them.

3. **⚠ LOW — `session_pnl` naming is misleading:** `fleet_intel["session_pnl"]` computes all-time P&L, not today's session. If the UI labels this as session P&L, users will see +$28.65 when today's actual session is -$17.31. Rename to `total_pnl` or fix the SQL to filter by today.

4. **INFO — DELTA strategy leaking on max-hold:** 65.6% of DELTA trades exit via max-hold timer. Net P&L on these exits is negative on average. Not a data bug, but a signal to review DELTA's entry logic or widen max-hold exits to target recovery.
