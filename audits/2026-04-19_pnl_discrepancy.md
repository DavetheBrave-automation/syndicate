# P&L Discrepancy Audit — 2026-04-19

**Gap:** Dashboard header shows **-$81.73** | Leaderboard sums to **-$69.69** | Difference: **+$12.04**

---

## Root Cause

Two different queries with different row filters.

### Header (`total_pnl` — app.py lines 359–362)

```python
total_pnl = round(sum(
    float(t.get("net_pnl") if t.get("net_pnl") is not None else t.get("pnl") or 0)
    for t in trades if int(t.get("id") or 0) > valid_from_id   # ← id > 33
), 2)
```

- Source: `_load_trades()` — fetches all rows, LIMIT 200, ordered by id DESC
- Filter: `id > 33` (valid_from_id from syndicate_meta)
- Scope: 84 trades (ids 34–117)
- Result: **-$81.73** ← **the correct number**

### Leaderboard (`_load_agent_stats()` — app.py lines 222–229)

```sql
SELECT agent_name,
       COUNT(*) as trades,
       SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END) as wins,
       ROUND(SUM(COALESCE(net_pnl, pnl, 0)), 2) as total_pnl
FROM syndicate_trades
WHERE exit_time IS NOT NULL
GROUP BY agent_name
```

- **No id filter.** Includes all 117 trades across all agents.
- Result: **-$69.69** ← inflated by pre-fix junk

### The $12.04 Explained

```
DB confirmed:
  SUM(net_pnl) WHERE id <= 33  =  +$12.04   ← pre-reconciliation trades (mixed quality)
  SUM(net_pnl) WHERE id >  33  =  -$81.73   ← post-fix valid dataset

  Leaderboard uses all 117: -$81.73 + $12.04 = -$69.69  ✓
```

The pre-fix trades (ids 1–33) date from before the April 15 data reconciliation
(`data_valid_from: 2026-04-15T20:47:05Z`). They collectively sum to **+$12.04 net_pnl**,
which inflates the leaderboard total by that amount.

---

## Verification

```sql
-- Header produces:
SELECT SUM(net_pnl) FROM syndicate_trades WHERE id > 33;
-- → -81.7284

-- Leaderboard produces:
SELECT SUM(COALESCE(net_pnl, pnl)) FROM syndicate_trades WHERE exit_time IS NOT NULL;
-- → -69.6919

-- Pre-fix trades (the gap):
SELECT COUNT(*), SUM(net_pnl) FROM syndicate_trades WHERE id <= 33;
-- → 33 trades, +$12.04
```

No NULL agent_names, no phantom records, no fee double-counting. Clean gap.

---

## Per-Agent Breakdown (all 117 trades)

| Agent   | Trades | net_pnl   | fees_paid |
|---------|--------|-----------|-----------|
| AXIOM   | 33     | +$25.70   | $20.41    |
| SHADOW  | 2      | +$4.23    | $0.25     |
| GHOST   | 3      | -$0.80    | $0.42     |
| ACE     | 6      | -$3.32    | $1.99     |
| PHOENIX | 3      | -$6.04    | $1.63     |
| DIAMOND | 3      | -$11.09   | $7.46     |
| DELTA   | 39     | -$30.51   | $21.86    |
| CIPHER  | 28     | -$47.87   | $32.00    |
| **ALL** | **117**| **-$69.69** | **$87.01** |

Note: `SUM(pnl)` (gross, pre-fee) = **+$17.32**. Fees of **$87.01** convert that to -$69.69.
CIPHER's 64% win rate with -$47.87 net = average loss >> average win (fee drag on small wins).

---

## Proposed Fix (pending David approval)

Add `WHERE id > ?` filter to `_load_agent_stats()` using `valid_from_id`, matching the header filter:

```python
def _load_agent_stats(valid_from_id: int = 0) -> dict[str, dict]:
    cur.execute("""
        SELECT agent_name, COUNT(*), ..., ROUND(SUM(COALESCE(net_pnl, pnl, 0)), 2)
        FROM syndicate_trades
        WHERE exit_time IS NOT NULL
          AND id > ?                          -- ← add this
        GROUP BY agent_name
    """, (valid_from_id,))
```

Then pass `valid_from_id` from `dashboard()` into `_load_agent_stats()`.
After this fix, leaderboard total will match the header: **-$81.73**.

**Do NOT implement without David's approval.**
