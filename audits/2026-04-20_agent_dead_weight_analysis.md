# Agent Dead-Weight Analysis
**Date:** 2026-04-18 (Monday label: 2026-04-20)
**Engine lifetime:** 2026-04-11T21:47Z → present  
**DB baseline:** 116 closed trades | Net P&L: -$70.58 | Total fees: $86.60  
**Markets scanned this session:** 1,023–1,166 | Active series: KXBTCD, KXETHD, KXPGAR3LEAD, KXPGATOUR  
**Gate log window:** PID 28300 only (started 2026-04-18T18:50Z — older gate sessions discarded output)

---

## Section 1 — Per-Agent Stats

| Agent | Signals | Last Signal | Trades | Net P&L | Win Rate | Avg Hold | Last Trade |
|-------|---------|-------------|--------|---------|----------|----------|------------|
| AXIOM | 72 | 2026-04-18T14:54Z | 33 | **+$25.70** | 51.5% | 9.2m | 2026-04-18T17:47Z |
| SHADOW | ~0 visible | — | 2 | +$4.23 | 100.0% | 0.3m | 2026-04-13T04:02Z |
| GHOST | 7 | 2026-04-18T10:57Z | 3 | -$0.80 | 33.3% | 4.7m | 2026-04-14T10:57Z |
| ACE | 26 | 2026-04-18T10:13Z | 6 | -$3.32 | 16.7% | 12.4m | 2026-04-14T16:03Z |
| PHOENIX | 10 | 2026-04-18T10:13Z | 3 | -$6.04 | 0.0% | 8.5m | 2026-04-13T11:53Z |
| DIAMOND | 381 | 2026-04-18T14:54Z | 3 | -$11.09 | 33.3% | 25.0m | 2026-04-18T18:33Z |
| DELTA | 137 | 2026-04-18T14:38Z | 39 | -$30.51 | 25.6% | 41.4m | 2026-04-18T19:28Z |
| CIPHER | 250 | 2026-04-18T14:38Z | 27 | **-$48.76** | 63.0% | 24.3m | 2026-04-18T17:47Z |
| BLITZ | **0** | — | 0 | — | — | — | — |
| ENDGAME | **0** | — | 0 | — | — | — | — |
| MIRROR | **0** | — | 0 | — | — | — | — |
| OIL | **0** | — | 0 | — | — | — | — |
| ORACLE | **0** | — | 0 | — | — | — | — |
| TIDE | **0** | — | 0 | — | — | — | — |
| SAGE | n/a | evaluating | 0 | n/a | n/a | — | — |
| ECHO | n/a | n/a | 0 | n/a | n/a | — | — |

**Note:** Signal counts reflect log search for `[AGENT] Signal` lines across full engine log (7.7MB). SAGE and ECHO are infrastructure agents — SAGE grades decision patterns, ECHO grades trades and writes blocks; neither emits entry signals.

---

## Section 2 — Gate Behavior (Current Session, PID 28300)

Gate log only covers post-18:50Z UTC. Historical gate sessions (covering ~90% of all trades) discarded output. Figures below are current-session only.

| Agent | TC Calls | EXECUTE | PASS | ECHO Blocked |
|-------|----------|---------|------|--------------|
| AXIOM | 7 | 0 | ~7 | 0 |
| CIPHER | 7 | 0 | ~6 | 1 |
| DELTA | 8 | 2 | ~6 | 1 |
| DIAMOND | 10 | 2 (1 open) | ~8 | 0 |

No other agents reached TC during the current gate session. The 20 PASS verdicts visible in the engine log span multiple sessions and are not fully agent-attributed (gate logs were not captured historically).

**Key gate observations:**
- CIPHER's 40-60 bucket is ECHO-blocked until 22:34 UTC today
- DELTA's 40-60 bucket is ECHO-blocked until 19:18 UTC tomorrow
- Both ECHO blocks were written automatically by echo.py from F-grade trades
- TC conversion rate for this session: EXECUTE on ~15% of calls (3 from 20 visible decisions)

---

## Section 3 — Silent Agent Diagnostics

### BLITZ — Velocity Fade Agent
**Strategy:** Buy YES on panic drops (vel < −15%), buy NO on euphoria spikes (vel > +15%).  
**Requirements:** `velocity < −15%` OR `velocity > +15%` AND `0.08 < yes_price < 0.75` AND `spread < 0.10`.  
**Why silent:** BTC has been range-bound this week. The scanner reports 26–57 velocity events per cycle, but none have hit the ±15% threshold in the engine log. The hot-path gate at `_VELOCITY_GATE = −12.0` filters markets before BLITZ even evaluates them — if velocity > −12%, the market is dropped. BLITZ fires only on extreme intra-session panic/euphoria.  
**Live check:** Engine scans ~1,100 KXBTCD/KXETHD markets every cycle. Zero BLITZ `[BLITZ] Signal` lines in the full engine log.  
**Verdict:** PARK — implementation correct, waiting for high-volatility catalyst (ETF news, macro shock, liquidation cascade).

---

### TIDE — Momentum Follower
**Strategy:** Follow confirmed 10-min directional momentum (vel_10m ≥ ±15%).  
**Requirements:** `abs(vel_10m) >= 15%` AND `0.10 < yes_price < 0.90` AND `days_to_settlement <= 1.5`.  
**Why silent:** 10-min velocity computation requires multiple price history points 600s apart. At the engine's 5-min heartbeat cadence this is achievable in theory, but sustained 15%+ momentum in either direction is not occurring in the current BTC/PGA market. Zero TIDE `[TIDE] Signal` lines in the log.  
**Note:** The 5-min window is explicitly commented as unavailable at heartbeat cadence — the agent correctly uses only the 10-min window.  
**Verdict:** PARK — implementation correct, waiting for trending market conditions.

---

### MIRROR — Post-Move Mean Reversion
**Strategy:** Enter opposite direction after large move that has now stabilized.  
**Requirements:** `max_move_in_15min >= 25%` AND `vel_2min < 3%` (stabilizing) AND `yes_price < 0.15` OR `yes_price > 0.85`.  
**Why silent:** Extremely specific three-part condition: a 25%+ swing must occur AND price must sit below 15¢ or above 85¢ AND the velocity must have settled. This combination requires a crash-then-stall or spike-then-stall in extreme territory. Not observed in current BTC market cycle.  
**Verdict:** PARK — implementation correct, pattern is rare by design. Will fire during flash crashes.

---

### ORACLE — Politics & Economics Specialist
**Strategy:** TC-assisted web-search trades on KXPOL, KXECON, KXFED, KXCPI, KXELEC, KXAPPROVAL.  
**Requirements:** Series in `ORACLE_SERIES` AND `0.05 < yes_price < 0.95` AND `days_to_settlement <= 14` AND `volume_dollars >= 1000`.  
**Why silent:** Confirmed via live scan — KXFED-26APR markets are in the scan but priced at **99.5¢** (YES = "will rate be at/below X%"). At that price level, `yes_price > 0.95` → ORACLE's GHOST-zone exclusion filters them. Result: 0 ORACLE-eligible markets (5¢–95¢ range) in the current scan.  
**Root cause:** The April Fed meeting is settled; remaining rate contracts are certainties either direction.  
**When it fires:** Next KXCPI or KXELEC series when market is pricing a genuinely uncertain outcome (5¢–95¢).  
**Verdict:** PARK — implementation correct, no suitable markets currently listed.

---

### OIL — WTI Crude Specialist
**Strategy:** Macro-driven overlay on KXWTIW and KXWTIMAX contracts.  
**Requirements:** `ticker.startswith("KXWTIW" or "KXWTIMAX" or "KXWTI")` AND `days_to_settlement <= 7` AND `0.25 < yes_price < 0.75`.  
**Why silent:** KXWTIW and KXWTIMAX series are not present in the live scan. Confirmed: unique series in agent eval heartbeat this session = {KXBTCD, KXETHD, KXPGAR3LEAD, KXPGATOUR}. No oil contracts.  
**Root cause:** Kalshi may have delisted or rolled the WTI weekly series, or they are below the engine's liquidity filter ($1,000 volume threshold).  
**Action needed:** Verify whether KXWTIW/KXWTIMAX are currently listed on Kalshi. If delisted, OIL is dead code until a new oil series appears.  
**Verdict:** PARK — implementation correct, underlying market series unavailable.

---

### ENDGAME — Tennis Final Set Specialist
**Strategy:** Edge trade at match point pressure in ATP/WTA final sets.  
**Requirements:** `series in {KXATPMATCH, KXWTAMATCH}` AND `game.is_final_set == True` AND `NOT game.is_match_point` AND `days_to_settlement <= 0.125` (3h) AND edge ≥ 15%.  
**Why silent:** ENDGAME requires live game state from `tennis_ws.match_game_to_ticker()`. Even if KXATPMATCH markets exist in the scan (GHOST had ACE/GHOST trades Apr 13–14), ENDGAME adds the additional constraint of being in a final set — which is maybe 30% of all matches. Currently no tennis matches pass the heartbeat eval (no KXATPMATCH in today's agent evals).  
**Verdict:** PARK — implementation correct, pattern is tournament-schedule-dependent.

---

### SHADOW — Top-Agent Mirror
**Strategy:** Delegate to the current top-performing agent per series, reduce conviction tier by one, re-brand signal as SHADOW.  
**Requirements:** Top agent's `should_evaluate()` passes AND top agent's `evaluate()` fires a signal.  
**Why silent (recently):** SHADOW IS being evaluated (visible in heartbeat: `agent=SHADOW ticker=KXBTCD-26APR1817-T75749.99`). But the top agent for KXBTCD is not firing a signal at each evaluation cycle, so SHADOW captures nothing. SHADOW's 2 historical trades (+$4.23, 100% WR) were both PGA tour quick-scalps on Apr 13 when the top PGA agent (AXIOM/ACE) fired.  
**Note:** SHADOW's `_instantiate_agent()` map excludes CIPHER, DELTA, ORACLE, TIDE, BLITZ, MIRROR, OIL — it only shadows from {ACE, AXIOM, PHOENIX, BLITZ, GHOST, ENDGAME, DIAMOND}. If those agents don't fire, SHADOW doesn't fire.  
**Verdict:** PARK — infrastructure dependency on primary agent signals. Will activate on next AXIOM/DIAMOND EXECUTE.

---

## Section 4 — Active Agent Analysis

### AXIOM — KEEP
**Signal activity:** 72 signals, daily active, last signal 14:54Z today.  
**Performance:** 33 trades, +$25.70, 51.5% WR, 9.2m avg hold. Only net-positive agent.  
**Pattern:** Multi-series (BTC + PGA golf). Consistent PROPHECY/HIGH_CONVICTION signals.  
**TC behavior:** Sends signals, TC passes most (exposure/tier management), EXECUTEs the cleanest setups.  
**Recommendation:** **KEEP** — sole profitable agent with meaningful sample size (n=33). Core engine driver.

---

### CIPHER — FIX
**Signal activity:** 250 signals, daily active, last signal 14:38Z today.  
**Performance:** 27 trades, **-$48.76**, **63.0% WR**, 24.3m avg hold.  
**Structural problem:** 63% win rate with net-negative P&L = catastrophically asymmetric sizing. When CIPHER loses, it loses big (scale was 165–400 contracts before cap). Wins are individually small (low-conviction tier exits at +21–23%). Net result: 17 wins are being wiped out by 10 losses.  
**Fix applied (2026-04-18):** Hard cap at 10 contracts at execution layer AND signal layer. This limits future loss per trade but doesn't fix the existing drawdown.  
**Remaining issue:** Even at 10 contracts, if CIPHER's edge is real (63% WR), it should eventually be net-positive with capped sizing. Need ≥30 trades post-cap to evaluate.  
**Recommendation:** **FIX** — cap applied, run in observation for 30+ more trades. If WR holds at 55%+ with capped sizing, it becomes net-positive. If WR degrades below 50% post-cap, consider RETIRE.

---

### DELTA — FIX
**Signal activity:** 137 signals, daily active, last signal 14:38Z today.  
**Performance:** 39 trades, **-$30.51**, 25.6% WR, 41.4m avg hold.  
**Problems identified:**
1. **Max-hold was 60min** (Fix B: now 240min for DELTA) — was cutting positions too early
2. **Low win rate (25.6%)** — may reflect edge misestimation or wrong market selection
3. **TC exit reviews help** — trades 115 (−$9.65 at −34.4%) and 116 (+$2.19 at +38%) both closed via TC review vs max-hold

**Fix B status:** Applied 2026-04-18 at execution layer. Not yet confirmed via 240-min hold in production (no DELTA position has survived past 60min under new engine).  
**Key risk:** 25.6% WR suggests the edge calculation itself may be wrong. Fix B alone won't turn the P&L positive if DELTA is entering bad setups.  
**Recommendation:** **FIX** — monitor for 20 more trades post-Fix B. If WR doesn't improve to ≥35%, escalate to full edge review.

---

### DIAMOND — FIX
**Signal activity:** 381 signals (highest of all agents), last signal 14:54Z today.  
**Performance:** 3 trades, **-$11.09**, 33.3% WR, 25.0m avg hold.  
**Critical finding:** 381 signals → 3 trades = 0.8% execution rate. Either:
1. TC is consistently passing on DIAMOND's setups (low TC trust in DIAMOND reasoning)
2. DIAMOND signals are hitting an upstream gate block (exposure, duplicate position, tier cap)
3. The n=3 sample is too small to evaluate performance

**Current session:** 10 TC calls for DIAMOND → 2 EXECUTE (20% conversion). This is better than lifetime average, suggesting TC is evaluating favorably when signals are clean.  
**Recent trade:** Apr 16–18 (3 trades). Prior to Apr 16 = zero trades despite hundreds of signals. Something changed on Apr 16 that let DIAMOND through.  
**Action needed:** Inspect TC's PASS reasoning for DIAMOND signals. Check whether DIAMOND's `conviction_tier` and `edge_pct` estimates are realistic enough to pass TC scrutiny.  
**Recommendation:** **FIX** — TC pass rate is the blocker, not signal quality. Need to inspect DIAMOND signal reasoning vs TC rejection pattern.

---

### GHOST — PARK
**Signal activity:** 7 signals, last 10:57Z today. Signals only on KXATPMATCH markets.  
**Performance:** 3 trades, -$0.80, 33.3% WR. Only traded Apr 13–14 when ATP Masters was active.  
**Why silent now:** GHOST targets extremely oversold contracts (yes_price < 5¢ territory on KXATPMATCH). Those markets aren't active today.  
**Recommendation:** **PARK** — correct implementation, waiting for next ATP/WTA calendar event on Kalshi.

---

### ACE — PARK (marginal)
**Signal activity:** 26 signals, last 10:13Z today. All on KXATPMATCH.  
**Performance:** 6 trades, -$3.32, 16.7% WR (1 win in 6).  
**Concern:** 16.7% WR over 6 trades is very poor. ACE's strategy (momentum scalp on ATP match markets) appears misfit to how Kalshi prices these contracts. The single win was likely a lucky gap.  
**Path:** If no ATP improvement in next 10 trades, escalate to RETIRE.  
**Recommendation:** **PARK** — small sample but early pattern is concerning. Reassess after next ATP tournament.

---

### PHOENIX — RETIRE
**Signal activity:** 10 signals lifetime, last 10:13Z today.  
**Performance:** 3 trades, -$6.04, **0.0% WR** (0 wins in 3).  
**Analysis:** Zero wins in 3 trades signals the strategy logic is wrong for current market conditions — not unlucky variance. PHOENIX is a reversal agent on ATP markets. The combination of (a) ATP markets not matching its entry conditions and (b) a 0% historical win rate provides no basis to continue.  
**Recommendation:** **RETIRE** — remove from active agent roster or disable `should_evaluate()` to return False until a complete strategy rewrite.

---

## Section 5 — Summary Table

| Agent | Category | Trades | Net P&L | Win Rate | Reason |
|-------|----------|--------|---------|----------|--------|
| AXIOM | **KEEP** | 33 | +$25.70 | 51.5% | Only profitable agent, consistent, active |
| CIPHER | **FIX** | 27 | -$48.76 | 63.0% | Cap applied — sizing was the bug, not edge |
| DELTA | **FIX** | 39 | -$30.51 | 25.6% | Fix B applied — win rate still low, monitor |
| DIAMOND | **FIX** | 3 | -$11.09 | 33.3% | TC conversion blocker — inspect pass reasoning |
| SHADOW | **PARK** | 2 | +$4.23 | 100% | Correctly implemented, depends on primary agents |
| GHOST | **PARK** | 3 | -$0.80 | 33.3% | ATP series dependent, correct implementation |
| ACE | **PARK** | 6 | -$3.32 | 16.7% | Small sample, concerning WR, reassess next ATP |
| BLITZ | **PARK** | 0 | — | — | Waiting for ±15% velocity event |
| TIDE | **PARK** | 0 | — | — | Waiting for sustained momentum |
| MIRROR | **PARK** | 0 | — | — | Waiting for 25%+ crash-then-stabilize pattern |
| ORACLE | **PARK** | 0 | — | — | No eligible markets (all KXFED at extremes) |
| OIL | **PARK** | 0 | — | — | KXWTIW/KXWTIMAX not in Kalshi scan |
| ENDGAME | **PARK** | 0 | — | — | No live ATP final-set matches |
| PHOENIX | **RETIRE** | 3 | -$6.04 | 0.0% | Zero wins, wrong strategy fit for ATP markets |
| SAGE | infra | 0 | — | — | Decision grader, not a trading agent |
| ECHO | infra | 0 | — | — | Trade grader + block writer, not a trading agent |

---

## Section 6 — Priority Actions

**Immediate (before next session):**
1. **CIPHER post-cap audit** — after 30 trades post-cap, reassess WR and sizing P&L
2. **DELTA Fix B confirmation** — need one DELTA position to exceed 60min without max-hold exit
3. **DIAMOND TC pass diagnosis** — pull 5 DIAMOND PASS decisions and inspect TC's stated reason

**Deferred:**
4. **PHOENIX disable** — add `return False` at top of `should_evaluate()` in phoenix.py; too much noise for zero wins
5. **OIL series verify** — check Kalshi API for whether KXWTIW series is currently listed
6. **ACE watchlist** — flag at 10 trades without WR improvement to 33%+

**Structural observation:**
Total gross fees ($86.60) exceed total net P&L loss ($70.58) — meaning even if every losing trade had broken even, fees alone would be the drag. At current trade volume, fee structure requires a consistent edge of ~4% per trade just to cover fees before profit. AXIOM is the only agent currently clearing that bar.
