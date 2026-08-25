# Baseline Snapshot — Pre Book Scanner + Maker Order Build
**Date:** 2026-04-18  
**Time:** ~22:10 UTC  
**Purpose:** Before-photo for Phase 1 implementation. Compare after book scanner + maker orders are live.

---

## Trade Database State

**Source:** `logs/syndicate_trades.db` → table `syndicate_trades`  
**Snapshot time:** 2026-04-18T22:10Z  
**Total trades:** 117

### Per-Agent Statistics

| Agent | Trades | Gross P&L | Fees Paid | Net P&L | Win Rate | Avg Hold |
|-------|--------|-----------|-----------|---------|----------|----------|
| AXIOM | 33 | +$46.12 | $20.41 | **+$25.70** | 51.5% | 9.2 min |
| SHADOW | 2 | +$4.48 | $0.25 | **+$4.23** | 100.0% | 0.3 min |
| GHOST | 3 | -$0.38 | $0.42 | **-$0.80** | 33.3% | 4.7 min |
| ACE | 6 | -$1.33 | $1.99 | **-$3.32** | 16.7% | 12.4 min |
| PHOENIX | 3 | -$4.41 | $1.63 | **-$6.04** | 0.0% | 8.5 min |
| DIAMOND | 3 | -$3.63 | $7.46 | **-$11.09** | 33.3% | 25.0 min |
| DELTA | 39 | -$8.65 | $21.86 | **-$30.51** | 25.6% | 41.4 min |
| CIPHER | 28 | -$14.87 | $33.00 | **-$47.87** | 64.3% | 23.6 min |

### All-Time Totals

| Metric | Value |
|--------|-------|
| Total trades | 117 |
| Gross P&L | +$17.32 |
| Total fees | $87.01 |
| **Net P&L** | **-$69.69** |
| Win rate | 42.7% |
| Fees / gross ratio | 5.02× |

**Key insight:** System is gross-profitable (+$17.32) but fee-negative (-$87.01). Under true maker pricing, portfolio would be +$17.32. This is the number to beat after Phase 1.

### Last 5 Trades

| ID | Agent | Ticker | Side | Net P&L | Exit Reason | Exit Time |
|----|-------|--------|------|---------|-------------|-----------|
| 117 | CIPHER | KXBTCD-26APR1817-T75749.99 | YES | +$0.89 | Target hit: +24% | 2026-04-18T20:42Z |
| 116 | DELTA | KXBTCD-26APR1817-T75999.99 | NO | +$2.19 | TC exit: P&L at 38% exceeds +30% HOLD bound | 2026-04-18T19:28Z |
| 115 | DELTA | KXBTCD-26APR1817-T75749.99 | NO | -$9.65 | TC exit: P&L -34.4% below -25% threshold | 2026-04-18T19:18Z |
| 114 | DIAMOND | KXETHD-26APR1817-T2349.99 | NO | +$4.49 | Target hit: +34% | 2026-04-18T18:33Z |
| 113 | DELTA | KXBTCD-26APR1817-T75999.99 | NO | +$4.24 | Target hit: +23% | 2026-04-18T18:28Z |

---

## Process State

**Engine:**  
- PID: 22096  
- Started: ~2026-04-18T13:44Z (watchdog restart #3)  
- Uptime at snapshot: ~8h26m  
- Memory: ~62MB  
- Status: HEALTHY — last log 2026-04-18T22:08Z  

**Gate:**  
- PID: 28764  
- Started: ~2026-04-18T13:50Z (watchdog restart #1)  
- Uptime at snapshot: ~8h20m  
- Memory: ~73MB  
- Status: HEALTHY — processing CIPHER + DIAMOND signals  

**Watchdog:**  
- PID: 27128  
- Started: ~2026-04-18T13:44Z  
- Uptime at snapshot: ~8h26m  
- Memory: ~16MB  
- Status: HEALTHY — last CRITICAL at 13:50Z (gate restart), clean since  

---

## Echo Blocks State

**Source:** `memory/echo_blocks.json`  
**Snapshot time:** 2026-04-18T22:10Z

| Block Key | Blocked Until | Reason | Source Trade |
|-----------|--------------|--------|-------------|
| CIPHER:KXBTCD:40-60 | 2026-04-18T22:34Z | F grade: pnl=$-48.38 on CIPHER KXBTCD 40-60 — manually written post-trade #108 (gap-down stop loss at 165 contracts unvalidated scale) | #108 |
| DELTA:KXBTCD:40-60 | 2026-04-19T19:18Z | F grade: pnl=$-9.65 on DELTA KXBTCD 40-60 | #0 (bug: trade ID not resolved) |

Note: CIPHER block expires in ~24 min from snapshot time. DELTA block expires ~21h from now.

---

## Current Config (Key Settings)

**Source:** `syndicate_config.yaml`  
**As of bench implementation:**

```yaml
active_agents: [AXIOM, SAGE, ECHO]   # NEW — bench applied

syndicate:
  paper_mode: true
  scan_interval_heartbeat: 300
  scan_interval_opportunity: 1800
  scan_interval_strategic: 21600

risk:
  max_per_trade_scalp: 1000.00
  max_total_exposure: 5000.00
  max_per_agent_exposure: 2000.00
  hard_stop_loss: 2000.00

scalper:
  max_hold_minutes: 60           # DELTA override: 240 via _AGENT_MAX_HOLD_DEFAULTS
  min_edge_pct: 7.0

conviction_sizing:
  GLITCH:          {min: 20, max: 50,  default: 25}
  HIGH_CONVICTION: {min: 50, max: 200, default: 100}
  PROPHECY:        {min: 200, max: 500, default: 300}

performance_tiers:
  probation: {max_position: 25}   # All 8 active agents on probation
  qualified:  {min_trades: 20, min_winrate: 0.50}
  proven:     {min_trades: 30, min_winrate: 0.60}
  elite:      {min_trades: 30, min_winrate: 0.75}

validation_phase:
  active: true
  benched_agents: []   # legacy field — superseded by top-level active_agents
```

---

## Market Scan State

**Source:** `triggers/heartbeat_latest.json` (2026-04-18T22:08Z)

| Metric | Value |
|--------|-------|
| Total markets scanned | 1,166 |
| Passed liquidity filter | 78 |
| AXIOM-eligible estimate | ~50 (BTC, ETH, PGA series) |
| Velocity events | 0 |

---

## What to Compare After Phase 1

When book scanner + maker orders are live, compare:

| Metric | Baseline | Target |
|--------|---------|--------|
| Net P&L | -$69.69 | > -$69.69 |
| Total fees | $87.01 | < $87.01 |
| Fee/gross ratio | 5.02× | < 5.02× |
| Avg fee per trade | $0.74 | < $0.74 |
| AXIOM net P&L | +$25.70 | > +$25.70 |
| Win rate | 42.7% | ≥ 42.7% (fill rate may reduce WR slightly) |
| Trade count | 117 | +N (AXIOM only, benched agents silent) |
| AXIOM fill rate | N/A (all taker) | Measure actual maker fill % |

**Leading indicator:** Fee-per-trade trending down is the first signal maker orders are working before enough trades to confirm P&L improvement.
