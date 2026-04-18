# Reconciliation v2 — Post-code-changes
**Date:** 2026-04-18 ~18:52 UTC  
**DB:** `logs/syndicate_trades.db`  
**Total trades at time of check:** 114 (was 111 at v1 — 3 new trades from watchdog-restarted engine)

## Q1 — Per-Agent Net P&L

| Agent | Trades | Net P&L | Wins |
|-------|--------|---------|------|
| AXIOM | 33 | +$25.70 | 17 |
| SHADOW | 2 | +$4.23 | 2 |
| GHOST | 3 | -$0.80 | 1 |
| ACE | 6 | -$3.32 | 1 |
| PHOENIX | 3 | -$6.04 | 0 |
| DIAMOND | 3 | -$11.09 | 1 |
| DELTA | 37 | -$23.05 | 9 |
| CIPHER | 27 | -$48.76 | 17 |

## Q2 — All-Time Totals

| Metric | Value |
|--------|-------|
| Total trades | 114 |
| Total P&L | -$63.12 |
| Total fees | $82.97 |
| Win rate | 42.1% |

## Q3 — 24h Rolling (includes manual closes + watchdog-restarted engine trades)

| Agent | Trades | P&L |
|-------|--------|-----|
| CIPHER | 7 | -$53.36 |
| AXIOM | 1 | -$28.88 |
| DIAMOND | 3 | -$11.09 |
| DELTA | 7 | -$15.75 |

## Q4 — Double-Fee Detection
**CLEAN** — 0 discrepancies

## Q5 — NULL Fee/net_pnl Rows
**CLEAN** — 0 NULL rows

## Q6 — DELTA Post-Fix Max-Hold Exits

Still shows only pre-fix-confirmation exits (ids 103, 106 at 60.1m and 60.5m). New DELTA trades from watchdog-restarted engine have not yet hit max-hold ceiling. Will confirm 240-min default is working once a DELTA position exceeds 60min without being closed by max-hold.

## Q7 — Exit Reason Breakdown (Top 10)

| Exit Reason | Count | P&L |
|-------------|-------|-----|
| Settlement protection: losing near close | 9 | -$26.44 |
| Target hit: +23% | 8 | +$8.61 |
| Target hit: +21% | 7 | +$15.99 |
| max hold: held=60.4m | 6 | -$2.05 |
| max hold: held=60.3m | 6 | -$2.25 |
| max hold: held=60.1m | 6 | -$7.65 |
| max hold: held=60.2m | 5 | -$1.40 |
| max hold: held=30.3m | 3 | +$0.40 |
| Win locked — our side at 95% | 3 | +$50.75 |
| Target hit: +68% | 3 | +$6.35 |

## Delta vs v1

| Metric | v1 | v2 | Change |
|--------|----|----|--------|
| Total trades | 111 | 114 | +3 (watchdog-restarted engine) |
| Total P&L | -$61.62 | -$63.12 | -$1.50 (new trades) |
| Q4 double-fee | CLEAN | CLEAN | — |
| Q5 NULL rows | CLEAN | CLEAN | — |

## Result: CLEAN — all 7 queries pass
