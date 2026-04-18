# Exit Handler Damage Assessment — 2026-04-17

**Query date:** 2026-04-17  
**Root cause:** `_process_agent_decision()` in main.py matched `*_exit_decision.json` and discarded any TC verdict that wasn't "EXECUTE". TC EXIT verdicts were silently logged as "discarding agent signal."

---

## Section 1: Confirmed Discarded EXIT Verdicts (Today)

From syndicate.log, April 17:

| Time (CDT as Z) | Ticker | TC Verdict | Discarded As |
|---|---|---|---|
| 10:50:46 | KXBTCD-26APR1712-T77599.99 | EXIT | "discarding agent signal" |

Only 1 confirmed EXIT verdict visible in logs (current gate session started at ~09:58 and previous gate logs are lost). The DIAMOND position entered at 10:47:22 and TC said EXIT at 10:50:46 — the position continued 2 more minutes before stop loss at 10:52:34 at -49%.

**Estimated P&L impact on trade id=105:**
- Entry: NO 74 contracts, YES=66¢ (NO cost=34¢)
- Stop loss trigger: YES=82.5¢ (NO at ~17.5¢), gross loss = -$12.21, net = -$14.90
- TC EXIT was at 10:50:46 — price likely between 70-78¢ YES (NO at 22-30¢)
- Conservative estimate: TC EXIT 2 min earlier would have saved ~$4-8 net on this trade
- Not quantifiable exactly without TC exit price in logs

---

## Section 2: All-Time Stop-Loss and Time-Exit Exposure

Trades that relied entirely on mechanical fallback because TC exit path was broken:

**Stop-loss exits:** 10 trades, **total net P&L = -$37.11**

These are the trades most likely to have been affected — they hit stop before time limit, suggesting price moved hard against the position. If TC had reviewed any of these and said EXIT before stop triggered, the loss could have been smaller.

| Exit Type | Count | Total Net P&L | Avg Hold |
|---|---|---|---|
| Stop loss (any %) | 10 | -$37.11 | ~13 min |
| Max hold time exceeded | 30 | -$13.12 | 55 min |
| Settlement protection | 9 | -$26.44 | 6.6 min |

**Total "fallback" exits:** 49 trades where TC judgment was unavailable.

---

## Section 3: What We Can't Know

The exact count of EXIT verdicts that were discarded is not reconstructible. The gate log (`wake_syndicate.log`) only captured the current session (14 lines from 09:58). Prior gate session logs were not redirected to file (gate was running in a terminal window).

The syndicate.log captured `[Gate] Agent decision: verdict=EXIT — discarding agent signal` entries, but `grep` only returns the one instance today because:
1. The gate was dead April 11-17 (no processing)
2. Gate restarted at 09:58 today
3. DIAMOND was the only trade today before this fix

---

## Section 4: Forward Impact (Fix Deployed)

With `_process_exit_decision()` now wired:

- TC EXIT verdicts call `order_manager.close_position()` immediately
- HOLD verdicts log `[TC Exit Review] HOLD: {ticker} {agent}` — clearly marked as a real decision
- 60s hysteresis cooldown prevents TC being called on every 30s scan cycle
- 2¢ price gate prevents TC calls when price hasn't moved
- `record_tc_exit_review()` on ScalperEngine tracks cooldown state per ticker

Expected effect: trades that TC wants to exit early (before target or stop) will now exit. This should reduce realized losses on positions where TC correctly identifies deteriorating setups.

---

## Section 5: Monday Note

**Sweeper TTL for lesson files** (separately): The sweeper cleaned up `diamond_lesson.json` (8 min old) before it could be manually backfilled. Add `*_lesson.json` to `_KEEP_ALWAYS` set in `main.py` or extend TTL. Today's lost lessons are: CIPHER (10:14 UTC) and DIAMOND (10:54 UTC). Self-heals next trade.
