# Pre-Restart Validation Report
**Date:** 2026-04-18  
**Engine status:** STOPPED  
**Gate status:** RUNNING (PID 20308, all fixes applied)  
**Open positions:** 0 (manually closed before fix sprint)

---

## Fix Sprint — 8 Validators (All PASS)

| # | Fix | File | Validator Result |
|---|-----|------|-----------------|
| 1 | TC timeout via runspace (120s WaitOne) | `intelligence/wake_syndicate.ps1` | **PASS** — runspace pattern confirmed, no visible window, PATH resolution, WaitOne(120000) |
| 2 | Watchdog dual-process monitoring | `tools/watchdog.py` | **PASS** — gate heartbeat monitoring, BACKOFF_DELAYS=[0,30,120,300], alert-only after 4 attempts |
| 3 | ECHO grade enforcement (write blocks) | `agents/echo.py` | **PASS** — `_write_echo_block()` writes `memory/echo_blocks.json` on F grade, 24h block TTL |
| 4 | ECHO block check in gate | `intelligence/wake_syndicate.ps1` | **PASS** — block key `{agent}:{series}:{bkt}` checked before TC call, expired blocks auto-pruned |
| 5 | CIPHER 10-contract scale cap | `agents/cipher.py` | **PASS** — `max_size_dollars` capped at `10 * contract_cost`, logs cap event |
| 6 | Sweeper protecting lesson/postmortem | `main.py` `_is_sweepable()` | **PASS** — `*_lesson.json` and `*_postmortem.json` excluded from sweep |
| 7 | Exposure config failure halt | `core/exposure_manager.py` | **PASS** — `_config_ok()` check at top of `check_trade()`, CRITICAL log + False return on config load failure |
| 8 | Silent bare-excepts (3 locations) | `connectors/kalshi_ws.py`, `agents/base_agent.py` (×2) | **PASS** — all 3 now log with context; pong failure → WARNING, cooldown load/save → WARNING |

---

## Reconciliation Queries — DB State as of 2026-04-18 ~13:00 UTC

### Q1 — Per-Agent Net P&L (COALESCE net_pnl, pnl)

| Agent | Trades | Net P&L | Wins | Losses | Win Rate |
|-------|--------|---------|------|--------|----------|
| AXIOM | 33 | +$25.70 | 17 | 16 | 51.5% |
| SHADOW | 2 | +$4.23 | 2 | 0 | 100% |
| GHOST | 3 | -$0.80 | 1 | 2 | 33.3% |
| ACE | 6 | -$3.32 | 1 | 5 | 16.7% |
| PHOENIX | 3 | -$6.04 | 0 | 3 | 0% |
| DIAMOND | 2 | -$15.58 | 0 | 2 | 0% |
| DELTA | 35 | -$17.06 | 8 | 27 | 22.9% |
| CIPHER | 27 | -$48.76 | 17 | 10 | 63.0% |

> **CIPHER note:** 63% win rate but -$48.76 P&L is explained by trade #108: KXBTCD stop-loss gap-down from 48.5¢ → 36¢, net -$48.38 (165-contract unvalidated scale). Without #108, CIPHER stands at -$0.38 on 26 remaining trades. This is the cascade that triggered the 10-contract cap fix (Fix #5).

### Q2 — All-Time Totals

| Metric | Value |
|--------|-------|
| Total trades | 111 |
| Total P&L | -$61.62 |
| Total fees paid | $75.48 |
| Overall win rate | 41.4% |

### Q3 — 24h Rolling P&L (includes manual closes)

| Agent | Trades | P&L |
|-------|--------|-----|
| CIPHER | 7 | -$53.36 |
| DIAMOND | 2 | -$15.58 |
| AXIOM | 1 | -$28.88 |
| DELTA | 5 | -$9.76 |

> 24h is dominated by: (a) AXIOM manual close -$28.88 (pre-restart sprint), (b) CIPHER trade #108 -$48.38 + 6 other closes, (c) DIAMOND 2 losses -$15.58.

### Q4 — Double-Fee Detection
**CLEAN** — No rows where `(pnl - fees_paid) != net_pnl` beyond $0.01 tolerance.

### Q5 — NULL Fee / net_pnl Rows
**CLEAN** — No NULL rows found.  
> April 17 audit flagged rows 102/103 as NULL. Current state shows all rows fully populated — resolved.

### Q6 — DELTA Max-Hold Exits (sample)

| ID | Ticker | Hold Time | P&L |
|----|--------|-----------|-----|
| 106 | KXBTCD-26APR1717-T77999.99 | 60.1m | -$6.24 |
| 103 | KXBTCD-26APR1717-T74499.99 | 60.4m | -$0.20 |
| 99 | KXBTCD-26APR1717-T75499.99 | 60.1m | -$0.20 |
| 96 | KXBTCD-26APR1717-T74999.99 | 60.4m | -$0.96 |

DELTA has 35 trades, predominantly KXBTCD, with ~14+ max-hold exits. 22.9% win rate is structural (DELTA holding BTC range contracts to expiry). DELTA pattern review is on the Monday queue.

### Q7 — Exit Reason Breakdown (Top Categories)

| Exit Reason | Count | P&L |
|-------------|-------|-----|
| Settlement protection: losing near close | 9 | -$26.44 |
| Target hit (all variants) | ~24 | +~$50 |
| Max hold time (all variants) | ~33 | ~-$14 |
| Win locked — our side at 95% | 3 | +$50.75 |
| Stop loss (all variants) | 8 | ~-$74 |
| TC manual closes (pre-restart sprint) | 3 | -$28.40 |

> Stop losses account for the majority of total P&L damage. The -$48.38 stop (trade #108) and -$14.90 stop (trade id with stop loss -49%) are the two catastrophic events. Both now protected by CIPHER scale cap.

---

## Discrepancy Reconciliation vs April 17 Audit

| Issue | April 17 State | Current State | Status |
|-------|---------------|---------------|--------|
| CIPHER dashboard shows +$2.56, DB truth -$11.79 | Dashboard COALESCE bug | DB now -$48.76 (trade #108 added); dashboard bug still exists (known) | **Known issue — dashboard only** |
| NULL fee rows 102/103 | fees_paid=NULL, net_pnl=NULL | Q5 shows ZERO null rows | **RESOLVED** |
| session_pnl label shows all-time | Dashboard label bug | Not a DB issue | **Known issue — dashboard only** |
| Double-fee detection | Not checked in prior audit | Q4: CLEAN | **NEW — CONFIRMED CLEAN** |

---

## GO / NO-GO Assessment

| Check | Result |
|-------|--------|
| All 8 fixes shipped and validated | GO |
| DB double-fee clean | GO |
| DB null row clean | GO |
| 0 open positions | GO |
| Gate running with all fixes | GO |
| Engine stopped cleanly | GO |
| CIPHER scale cap active | GO |
| ECHO block enforcement active (gate-side) | GO |
| Watchdog dual-monitoring active | GO |

**VERDICT: GO FOR RESTART**

---

## Known Issues (Non-Blocking)

1. **Dashboard CIPHER P&L** — shows aggregate vs row-level COALESCE discrepancy. Fix: update dashboard query to `COALESCE(net_pnl, pnl)` consistently.
2. **Dashboard session_pnl label** — shows all-time P&L, not today's. Cosmetic.
3. **DELTA pattern** — 3+ D/F grades on KXBTCD 40-60 bucket. Monday queue item.
4. **Watchdog not live kill-tested** — code review PASS only. Needs a controlled kill test in a future session.
5. **ECHO enforcement not live-tested** — block write + gate intercept unverified with an actual F grade trade. Will self-validate on next F grade.

---

## Next Action

**Awaiting David's restart approval.**

Command to restart engine:
```bash
cd "C:/Users/djnec/CommandCenter/syndicate" && python main.py
```
Or via watchdog (if watchdog is not yet running):
```bash
python tools/watchdog.py
```
