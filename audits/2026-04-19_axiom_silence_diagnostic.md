# AXIOM Silence Diagnostic — 2026-04-19

**Claim:** No signals from AXIOM since ~5:44 PM CT Saturday (PGA/Scheffler trade).
**Finding:** AXIOM is NOT silent at the engine level. TC is systematically blocking every signal.

---

## What AXIOM Has Been Doing

### Engine-level signals submitted (today, since 08:52 UTC)

| Time (UTC) | Ticker | Side | Edge | Conv |
|------------|--------|------|------|------|
| 08:52:58 | KXBTCD-26APR1917-T75499.99 | NO @ 29¢ | 23.6% | HIGH_CONVICTION |
| 08:52:59 | KXBTCD-26APR1917-T76499.99 | YES @ 29¢ | 8.3% | HIGH_CONVICTION |
| 08:58:18 | KXPGATOUR-RBH26-SSCH | YES @ 26¢ | 16.5% | HIGH_CONVICTION |
| 09:09:01 | KXBTCD-26APR1917-T76249.99 | YES @ 27¢ | 18.7% | HIGH_CONVICTION |
| 09:25:05 | KXBTCD-26APR1917-T75499.99 | NO @ 29¢ | 22.2% | HIGH_CONVICTION |
| 09:30:28 | KXBTCD-26APR1917-T76499.99 | YES @ 27¢ | 15.8% | HIGH_CONVICTION |
| 09:29:34 | KXPGATOUR-RBH26-SSCH | YES @ 26¢ | 16.7% | HIGH_CONVICTION |

AXIOM's `should_evaluate` is passing. Edge calculations are well above the 7% minimum.
AXIOM is also sweeping overnight: signals were submitted at 21:35, 21:57, 22:12, 22:28, 22:39, 23:00 UTC Saturday.

The "16 hours of silence" from the dashboard/live_feed perspective is real — but the cause is NOT AXIOM failing to find setups. It's TC rejecting every signal at the Gate.

---

## What the Gate Is Doing

`wake_syndicate.log` shows the same pattern repeating for every AXIOM signal:

```
[Syndicate Gate] Agent signal: axiom_signal.json | agent=AXIOM
[Syndicate Gate] Invoking TC for AXIOM (max 120s)...
[Syndicate Gate] Decision written: axiom_decision.json
[Syndicate Gate] Agent flow done for AXIOM.
```

TC responds, Gate writes `triggers/axiom_decision.json`, main.py reads it. No trade entered. Cycle repeats in ~5 minutes.

---

## TC's Actual Decision (tc_analysis.txt — last response)

```json
{
  "decision": "PASS",
  "side": "yes",
  "conviction": 1,
  "bet_size": 0.00,
  "entry_price": 0.265,
  "target_exit_price": 0.336,
  "exit_trigger": "N/A — no trade",
  "reasoning": "Settlement 26APR (7 days out) violates the 3-day rule.
                SAGE win_rate=35% on this pattern is below the 40% PASS threshold.
                Market prices YES at 26.5% — consensus is firmly NO, and buying YES
                here opposes consensus at an extreme price. Three rules stack against
                this trade.",
  "rule_used": "Only trade when settlement is within 3 days; PASS when SAGE win_rate < 40%"
}
```

**Two errors in TC's reasoning:**

### Error 1 — Wrong Settlement Date

TC reads `KXPGATOUR-RBH26-SSCH` → sees "26APR" embedded in the ticker → concludes "Settlement April 26, 7 days out" → "violates 3-day rule."

But AXIOM's engine computed `days=1` from the live market data (Kalshi's actual settlement timestamp). The RBC Heritage 2026 final round is today/tomorrow, not April 26. TC is parsing the ticker string rather than trusting the agent's reported days value.

### Error 2 — Wrong Agent Referenced

TC's reasoning cites `"SAGE win_rate=35%"` and a `"SAGE win_rate < 40% PASS threshold"`. This is AXIOM's gate evaluation, not SAGE's. TC is applying SAGE-specific performance filters to an AXIOM signal.

This suggests the TC prompt for AXIOM either:
- (a) Includes SAGE's memory/rules and TC is cross-contaminating
- (b) TC is hallucinating SAGE context when it cannot find a strong AXIOM-specific rejection reason

Main.py only acts on `"EXECUTE"` or `"REDUCE"`. TC returning `"PASS"` results in no trade, every time.

---

## AXIOM's `should_evaluate` Breakdown

No AXIOM `should_evaluate` failures are logged — AXIOM is passing all gates:

| Gate | Requirement | Status |
|------|-------------|--------|
| Not benched | active_agents = [AXIOM, SAGE, ECHO] | ✅ passes |
| Not tennis | series ∉ {KXATPMATCH, KXWTAMATCH} | ✅ passes |
| days_to_settlement ≤ 3 | BTC: ~0 days, PGA: ~1 day | ✅ passes |
| Not expiring < 2hr | BTC: 8+ hours, PGA: 1+ day | ✅ passes |
| volume_dollars ≥ 1000 | BTC volume >> $1k | ✅ passes |
| spread ≤ 0.04 | confirmed by scanner pass | ✅ passes |
| price in 0.25–0.75 | BTC at 0.26–0.29, PGA at 0.26 | ✅ passes |
| price outside 0.30–0.70 | 0.26 < 0.30 = extreme | ✅ passes |

All gates pass. AXIOM's edge is real. The block is entirely TC.

---

## Historical AXIOM Performance (33 trades, all valid)

- Net PnL: **+$25.70** — only profitable agent in the fleet
- Win rate: **52%** (17/33)
- Tier: **QUALIFIED** (20+ trades, 52% WR > 50% threshold)
- Last trade: KXPGATOUR-RBH26-SSCH entry at 5:44 PM CT Saturday (still open in live_feed)

The Scheffler position is showing in live_feed as an open entry with `pnl: null`. If the engine lost paper position state during the 08:52 UTC restart today, the position may be orphaned (no longer tracked but still open on paper).

---

## Verdict

| Question | Answer |
|----------|--------|
| Is AXIOM should_evaluate returning True? | ✅ Yes — regularly |
| Is edge calculation working? | ✅ Yes — 8%–24% edges computed correctly |
| Is AXIOM generating signals? | ✅ Yes — 7 signals since 08:52 UTC today |
| Is the Gate invoking TC? | ✅ Yes — every signal |
| Is TC approving any signals? | ❌ No — returning "PASS" every time |
| Is TC's reasoning correct? | ❌ No — wrong settlement date, wrong agent stats |

**This is not a market availability issue or a filter bug. TC is the blocker.**

---

## Proposed Next Steps (pending David approval)

1. **Immediate**: Review tc_analysis.txt after the next AXIOM gate invocation. Confirm TC is consistently misreading the ticker date.

2. **Prompt fix**: The gate prompt for AXIOM should explicitly pass `days_to_settlement` as a numeric value (from the signal JSON), not rely on TC to parse it from the ticker string.

3. **Agent isolation**: Confirm TC's prompt for AXIOM does not include SAGE memory/rules. The SAGE reference in TC output suggests cross-contamination in the prompt construction.

4. **Scheffler position check**: Confirm whether KXPGATOUR-RBH26-SSCH is still tracked as an open paper position or was orphaned by the 08:52 restart.

**Do NOT change AXIOM logic. Bug is in TC prompt/reasoning, not AXIOM.**
