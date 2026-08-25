# Expert Advisor Architecture — Specification
**Status:** Spec only — no code changes  
**Date:** 2026-04-18  
**Author:** TC  
**Scope:** Design document for phased implementation of domain expert layer

---

## 1. Problem Statement

The current architecture conflates two distinct jobs inside the trading agent:

1. **Market awareness** — does this domain look favorable right now? (BTC in freefall, CPI hot, tournament concluded)
2. **Signal detection** — is this specific contract mispriced right now?

Agents currently handle both, which means:
- Every `evaluate()` call re-derives macro context from scratch (or relies on the 5-min `aggregate.py` snapshot, which has no domain depth)
- ORACLE sends TC to web-search live polling data on every signal — expensive and slow
- CIPHER has no BTC order-book awareness — it only sees Kalshi's implied probability
- Sports agents have zero lookahead — they evaluate a market for a match that settled 2 hours ago

The **Expert Advisor Layer** decouples these responsibilities. Experts run continuously in the background, maintain a live domain view, and serve trading agents a pre-computed verdict at consult time. Agents focus on pattern detection; experts provide context.

---

## 2. Core Design Principles

1. **Separation of concerns** — experts do research, agents detect patterns, TC makes final calls
2. **Non-blocking** — expert consultation has a hard timeout (`10s default`); agents proceed without expert if unavailable
3. **Advisory by default, veto by exception** — experts advise; hard vetoes only for objective disqualifiers (settled contract, stale data flag)
4. **File-based IPC** — consistent with existing architecture; state written to `experts/state/` atomically
5. **Graded by ECHO** — expert verdicts are logged alongside trade outcomes; ECHO eventually grades expert accuracy

---

## 3. File Layout

```
syndicate/
├── experts/
│   ├── base_expert.py          # Abstract base class — background thread, state write, consult()
│   ├── expert_bus.py           # Dispatch routing: expert_bus.consult(expert="CRYPTO", signal=...)
│   ├── crypto_expert.py        # Phase 2
│   ├── macro_expert.py         # Phase 1
│   ├── commodities_expert.py   # Phase 3
│   ├── sports_expert.py        # Phase 4
│   └── state/
│       ├── crypto_view.json    # Written every 60s, atomic
│       ├── macro_view.json     # Written every 5 min, atomic
│       ├── commodities_view.json
│       └── sports_view.json
├── memory/
│   ├── crypto_expert_memory.json   # Long-term learning (existing memory/ dir)
│   ├── macro_expert_memory.json
│   ├── commodities_expert_memory.json
│   └── sports_expert_memory.json
└── experts/learning/           # Structured pre-fetch data
    ├── mlb/{date}/{game_id}_preview.json
    ├── atp/{tournament}/{match_id}_stats.json
    └── wta/{tournament}/{match_id}_stats.json
```

Rationale for `experts/state/` vs `memory/`: state files are ephemeral rolling views (overwritten every refresh cycle). Memory files are persistent cross-session learning (same as agents use in `memory/`).

---

## 4. BaseExpert Class (`experts/base_expert.py`)

```python
class BaseExpert(ABC):
    name: str               # "CRYPTO", "MACRO", etc.
    refresh_seconds: int    # How often to refresh view

    # -- Background loop --
    def start(self) -> None
        # Spawns daemon thread: _refresh_loop()
        # Writes initial state immediately, then every refresh_seconds

    def _refresh_loop(self) -> None
        # while True: refresh_view() → _write_state() → sleep(refresh_seconds)

    @abstractmethod
    def refresh_view(self) -> dict
        # Pull APIs, compute view, return dict

    def _write_state(self, view: dict) -> None
        # Atomic write: .tmp → os.replace()
        # Identical pattern to echo._write_echo_block()

    def _load_state(self) -> dict | None
        # Read state file, return None if missing/stale

    # -- Consultation --
    def consult(self, signal: dict, timeout_s: float = 10.0) -> dict | None
        # 1. Load current state (from file — avoids thread-safety issues)
        # 2. Call _evaluate_signal(state, signal) in a thread with timeout
        # 3. Return ConsultResult or None on timeout

    @abstractmethod
    def _evaluate_signal(self, state: dict, signal: dict) -> dict
        # Domain-specific verdict logic
        # Returns: {"conviction": 0-100, "narrative": str, "hard_veto": bool, "veto_reason": str|None}

    # -- Memory --
    def load_memory(self) -> dict
        # Reads memory/{name}_expert_memory.json — same pattern as BaseAgent

    def save_memory(self, mem: dict) -> None
        # Atomic write — same pattern as BaseAgent

    def record_outcome(self, signal: dict, verdict: dict, outcome: dict) -> None
        # Called by ECHO after trade closes
        # Logs: signal hash, conviction given, actual P&L, win/loss
        # Updates accuracy stats in memory file
```

### ConsultResult schema

```json
{
  "conviction": 72,
  "narrative": "BTC order book balanced, funding rate neutral. Signal is credible.",
  "hard_veto": false,
  "veto_reason": null,
  "state_age_seconds": 34,
  "expert": "CRYPTO"
}
```

`hard_veto: true` is only set for objective disqualifiers — never for "I think this is a bad trade." Examples of valid hard veto conditions:
- Contract already settled (verified via API or data timestamp)
- Data is stale beyond acceptable threshold and no fallback exists
- Explicit structural conflict (e.g., expert detects contract references tomorrow's game but match concluded today)

**Hard veto is NOT valid for:** "I think BTC is going down" or "macro looks unfavorable." Those produce `conviction < 30` — the agent sees a low-conviction advisory but still decides.

---

## 5. ExpertBus (`experts/expert_bus.py`)

Single module-level dispatch point. Agents call one function; expert routing is hidden.

```python
# expert_bus.py

_registry: dict[str, BaseExpert] = {}

def register(expert: BaseExpert) -> None:
    _registry[expert.name] = expert

def consult(expert: str, signal: dict, timeout_s: float = 10.0) -> dict | None:
    """
    Returns ConsultResult or None if expert unavailable/timeout.
    Agents treat None identically to UNAVAILABLE conviction.
    """
    exp = _registry.get(expert.upper())
    if exp is None:
        logger.warning("[ExpertBus] Expert %s not registered", expert)
        return None
    try:
        return exp.consult(signal, timeout_s=timeout_s)
    except Exception as e:
        logger.warning("[ExpertBus] Expert %s consult error: %s", expert, e)
        return None

def start_all() -> None:
    for exp in _registry.values():
        exp.start()
```

ExpertBus is initialized in `main.py` startup alongside scan_engine. Experts start their background threads during `main()` setup.

---

## 6. Agent Integration Protocol

Integration point: inside `evaluate()`, **after** the agent's internal pattern checks pass, **before** `submit_signal()`.

```python
# In agent evaluate() — example for CipherAgent
def evaluate(self, market, game=None) -> None:
    # ... existing pattern logic ...
    # At this point: candidate signal is built, passes internal filters

    # Expert consultation
    from experts.expert_bus import consult as expert_consult
    expert_response = expert_consult("CRYPTO", signal=candidate_signal, timeout_s=10)

    if expert_response is None:
        # Expert offline/timeout — proceed with caution flag
        candidate_signal["signal"]["expert_verdict"] = "UNAVAILABLE"
        candidate_signal["signal"]["expert_conviction"] = None

    elif expert_response.get("hard_veto"):
        # Objective disqualifier — abort cleanly
        logger.info(
            "[%s] Expert hard veto: %s", self.name, expert_response["veto_reason"]
        )
        return

    elif expert_response["conviction"] < 30:
        # Expert strongly disagrees — abort; log for ECHO grading
        logger.info(
            "[%s] Expert %s low conviction (%d) — signal dropped: %s",
            self.name, expert_response["expert"],
            expert_response["conviction"], expert_response["narrative"],
        )
        return

    else:
        # Advisory attached — TC will see it
        candidate_signal["signal"]["expert_verdict"] = expert_response["conviction"]
        candidate_signal["signal"]["expert_context"] = expert_response["narrative"]
        candidate_signal["signal"]["expert_state_age"] = expert_response["state_age_seconds"]

    self.submit_signal(candidate_signal)
```

**Conviction thresholds (proposed):**

| Range | Interpretation | Agent action |
|-------|---------------|--------------|
| 0–29 | Expert strongly disagrees | Drop signal |
| 30–49 | Expert skeptical | Attach context, let TC decide — TC may downgrade tier |
| 50–74 | Expert neutral/mild agreement | Attach context |
| 75–100 | Expert confirms signal | Attach context — TC may upgrade tier |

The 30-point hard floor is the only agent-side gate. All other conviction levels are informational for TC.

---

## 7. Expert Specifications

### 7.1 MacroExpert (Phase 1)

**Name:** `MACRO`  
**Refresh:** 300s (5 min)  
**State file:** `experts/state/macro_view.json`  
**Advises:** ORACLE (primary), secondary input field for CryptoExpert during Fed weeks

**Data sources:**
| Source | Data | Existing? | Auth |
|--------|------|-----------|------|
| FRED | Fed funds rate, yield curve, DXY | YES — `signals/fred.py` | Wired |
| BLS CPI | CPI releases, core PCE | No | Free, needs registration |
| Fed economic calendar | Meeting dates, dot plots | No | FRED series `FOMC` |
| FNG | Fear & Greed index | YES — `signals/fng.py` | Wired |
| MacroLLM | Narrative overlay | YES — `signals/macro_llm.py` | Wired |

**View schema (`macro_view.json`):**
```json
{
  "refreshed_at": "2026-04-18T14:35:00Z",
  "fed_rate": 4.33,
  "fed_status": "HAWKISH",
  "next_fed_meeting": "2026-05-07",
  "days_to_next_fed": 19,
  "cpi_last": 3.5,
  "cpi_trend": "FALLING",
  "next_cpi_release": "2026-05-14",
  "yield_curve": "INVERTED",
  "dxy": 104.2,
  "dxy_status": "RISING",
  "fng": 31,
  "fng_status": "FEAR",
  "macro_regime": "HAWKISH_TIGHTENING",
  "macro_narrative": "Fed on hold, CPI above target, markets pricing 1 cut in 2026.",
  "sources_ok": ["fred", "fng", "macro_llm"],
  "sources_failed": []
}
```

**`_evaluate_signal()` logic for ORACLE:**
- Load current view
- Check `days_to_next_fed` — if ≤ 2 and signal is KXFED series: return conviction 20 ("Fed meeting imminent, spread unpredictable")
- Check `cpi_trend` against KXCPI contract direction
- Check `macro_regime` against signal direction
- Build narrative summarizing agreement/disagreement

**Advantage over current state:** ORACLE currently sends TC to web-search every signal. With MacroExpert caching a view every 5 min, TC gets pre-computed macro context embedded in the signal. TC still decides — but cold web searches are replaced by warm cached data 95% of the time.

---

### 7.2 CryptoExpert (Phase 2)

**Name:** `CRYPTO`  
**Refresh:** 60s  
**State file:** `experts/state/crypto_view.json`  
**Advises:** AXIOM, CIPHER, DELTA, DIAMOND

**Data sources:**
| Source | Data | Existing? | Auth |
|--------|------|-----------|------|
| Coinbase Advanced Trade REST | BTC/ETH order book, recent trades, funding rate | No | David has keys |
| CoinMarketCap API | Market cap, dominance, volume rank | No | Free tier, needs key |
| Fleet bots (Eagle/Nano/Osprey) | Internal signals, position data | TBD — need IPC design | Internal |
| Kalshi BTC flow | Implied vol from price action | Derived — no new API | Computed |

**View schema (`crypto_view.json`):**
```json
{
  "refreshed_at": "2026-04-18T14:35:00Z",
  "btc_price": 75420.50,
  "btc_1h_change_pct": -0.8,
  "btc_24h_change_pct": -2.1,
  "btc_funding_rate": 0.0012,
  "btc_funding_regime": "NEUTRAL",
  "btc_ob_bid_ask_ratio": 0.92,
  "btc_dominance": 54.2,
  "eth_price": 2345.10,
  "eth_1h_change_pct": -1.1,
  "market_regime": "RANGING",
  "cmc_fear_rank": "FEAR",
  "fleet_signals": {},
  "kalshi_btc_implied_vol": "MODERATE",
  "sources_ok": ["coinbase", "cmc"],
  "sources_failed": []
}
```

**`_evaluate_signal()` logic for AXIOM/CIPHER/DELTA/DIAMOND:**
- If `market_regime == "RANGING"` and signal is momentum-based: conviction penalty
- If `btc_funding_rate > 0.02` (overheated longs): bearish bias for YES signals
- If `btc_ob_bid_ask_ratio < 0.80` (heavy ask pressure): conviction penalty on YES
- If `btc_1h_change_pct < -3%` and signal direction is YES: conviction 20 ("momentum against you")

**Fleet bot integration note:** IPC design is unresolved. Options: (a) fleet bots write a signal file that CryptoExpert reads, (b) REST endpoint on fleet bots, (c) shared Redis cache. Decision deferred to Phase 2 scoping.

---

### 7.3 CommoditiesExpert (Phase 3)

**Name:** `COMMODITIES`  
**Refresh:** Daily (EIA releases Wednesdays) + event-triggered on OPEC calendar  
**State file:** `experts/state/commodities_view.json`  
**Advises:** future `CommodityOracle` agent (not yet built)

**Data sources:**
| Source | Data | Auth |
|--------|------|------|
| EIA API | Weekly petroleum status, WTI price, inventory | Free, needs key |
| OPEC meeting calendar | Meeting dates, production decisions | Scraped or manual |
| Fed/DXY regime | Oil/dollar correlation | Via MacroExpert view |

**Note:** No trading agent currently consults this expert — CommoditiesExpert and CommodityOracle are co-dependent. Build both together in Phase 3 or neither.

**Phase 3 entry criteria:** OIL agent currently silent because KXWTIW/KXWTIMAX markets are not active. Before building CommoditiesExpert, verify that a tradeable oil market exists on Kalshi or is forthcoming.

---

### 7.4 SportsExpert (Phase 4)

**Name:** `SPORTS`  
**Refresh:** Schedule data daily at 6 AM; in-game state every 5 min for active matches  
**State file:** `experts/state/sports_view.json`  
**Advises:** ACE, GHOST, PHOENIX, ENDGAME

**Key capability — 24h lookahead:**  
SportsExpert pre-fetches upcoming matches, player stats, and head-to-head records **before** contracts appear in the Kalshi scan. When an agent encounters a KXATPMATCH contract, the matchup context is already cached — no cold API call needed.

**Data sources:**
| Source | Data | Auth |
|--------|------|------|
| MLB Stats API | Schedule, lineups, pitcher stats, park factors | Free, no auth |
| NBA API | Schedule, player stats, injury report | Free, no auth |
| ATP/WTA feeds | Tournament draw, rankings, match scores | Research needed — see open questions |
| ESPN undocumented | Real-time scores, in-progress states | Fragile — last resort |

**Learning folder structure:**
```
experts/learning/
  mlb/{YYYY-MM-DD}/{game_id}_preview.json     # Pre-built 24h before first pitch
  atp/{tournament_slug}/{match_id}_stats.json  # Updated during match
  wta/{tournament_slug}/{match_id}_stats.json
```

**`_evaluate_signal()` logic for ACE/GHOST/PHOENIX:**
- Load cached matchup data for the signal's ticker
- Compare Kalshi implied probability vs stats-derived true probability
- If matchup data is missing (match not pre-fetched): return conviction 40 ("no data — advisory only")
- If match already concluded (from learning data timestamp): return hard veto

**Sports data — ATP feed decision:** MLB and NBA are free and reliable. ATP/WTA real-time feeds are either paid or require scraping. Initial recommendation: start with MLB only in Phase 4. ATP integration deferred until feed source is confirmed.

---

## 8. Latency Budget

Each `evaluate()` call that reaches expert consultation must complete within the scanner's event loop budget.

| Step | Typical latency | Worst case |
|------|----------------|------------|
| Agent internal pattern check | <1ms | <5ms |
| `expert_bus.consult()` — load state file | <2ms | <10ms (cold disk) |
| `_evaluate_signal()` — pure dict logic | <1ms | <5ms |
| Total expert overhead | **<5ms** | **<20ms** |
| Expert timeout (no response) | 10s cap | Fall through with UNAVAILABLE flag |

State files are pre-written by background threads — `consult()` reads a file, not a live API. This is the critical design choice that keeps consultation fast.

**The only latency risk** is the first consult before the first refresh cycle completes. Mitigation: `BaseExpert.start()` calls `refresh_view()` synchronously before spawning the background thread, so state is populated before any agent consults.

---

## 9. Failure Modes and Fallbacks

| Failure | Behavior |
|---------|----------|
| Expert API down during refresh | Log warning, retain previous state file, set `sources_failed` field |
| State file missing (first start or crash) | `_load_state()` returns None → `consult()` returns None → agent proceeds with `expert_verdict: UNAVAILABLE` |
| State file stale > 3× refresh interval | `consult()` returns `{"conviction": 50, "narrative": "stale data", ...}` — neutral, not veto |
| `_evaluate_signal()` raises exception | Caught in `consult()`, returns None, logged |
| Consult timeout (>10s) | Agent proceeds with UNAVAILABLE flag — identical to API-down path |

Agents must never crash or stall due to an expert failure. Expert layer is advisory infrastructure, not a dependency.

---

## 10. ECHO Grading Integration

After a trade closes, ECHO already grades the trade (A–F). Expert verdict grading adds one additional column:

```python
# In echo.grade_trade() — new field
expert_verdict = trade_record.get("expert_verdict")   # conviction 0-100 or "UNAVAILABLE"
expert_conviction = trade_record.get("expert_conviction")
if expert_verdict and expert_verdict != "UNAVAILABLE":
    self._record_expert_accuracy(
        expert_name=signal.get("expert"),
        conviction_given=expert_conviction,
        trade_won=(net_pnl > 0),
    )
```

Expert accuracy is tracked in `memory/{expert_name}_expert_memory.json`:
```json
{
  "accuracy": {
    "conviction_buckets": {
      "75-100": {"calls": 12, "wins": 9, "win_rate": 0.75},
      "50-74":  {"calls": 8,  "wins": 4, "win_rate": 0.50},
      "30-49":  {"calls": 3,  "wins": 1, "win_rate": 0.33}
    },
    "hard_vetos": {"issued": 2, "correct": 2}
  }
}
```

This enables future calibration: if CryptoExpert's conviction 75+ only correlates with 50% win rate, the threshold logic gets adjusted.

---

## 11. Phased Rollout

### Phase 1 — Macro foundation (this week)
1. Build `base_expert.py` — background thread, state file I/O, `consult()`, memory
2. Build `expert_bus.py` — register/dispatch/`start_all()`
3. Build `MacroExpert` — wraps existing `signals/fred.py`, `signals/fng.py`, `signals/macro_llm.py`; adds BLS CPI calendar
4. Wire ORACLE to consult MacroExpert before `submit_signal()`
5. Validate: ORACLE signal appears in log with `expert_verdict` field attached
6. Add `expert_verdict` to TC prompt template so TC sees the expert context

**Phase 1 success criteria:** One ORACLE signal reaches TC with embedded MacroExpert context in the signal JSON. TC uses it without needing a fresh web search.

### Phase 2 — Crypto layer (next week)
1. Build `CryptoExpert` — Coinbase + CoinMarketCap + derived Kalshi flow
2. Resolve fleet bot IPC design
3. Wire AXIOM, CIPHER, DELTA, DIAMOND
4. Monitor: CIPHER and DELTA signal counts should decrease (low-conviction expert blocks some signals before TC)

**Phase 2 success criteria:** At least one CIPHER signal dropped by CryptoExpert low-conviction advisory (logged), and at least one EXECUTE signal carries expert context ≥ 70 conviction.

### Phase 3 — Commodities (deferred)
- Prerequisite: KXWTIW or equivalent oil series active on Kalshi
- Build CommoditiesExpert + CommodityOracle agent together

### Phase 4 — Sports (deferred)
- Build SportsExpert with MLB lookahead
- Wire ACE/GHOST/PHOENIX
- ATP/WTA feed source confirmed before starting

---

## 12. Open Questions for David

**Q1 — Hard veto scope:**  
Spec proposes hard veto ONLY for objective disqualifiers (settled contract, stale data, structural impossibility). All other disagreements produce low-conviction advisory. Do you want any exceptions — e.g., should CryptoExpert be able to hard-block CIPHER during a confirmed BTC flash crash?

*Recommendation: Keep hard veto narrow. TC already has final say. Two veto layers (expert + TC) is cleaner than three.*

**Q2 — Consultation cost / latency:**  
Every signal that passes agent internal filters will now call `expert_bus.consult()` before hitting TC. File-read latency is <5ms. But if expert consultation drops 40% of signals before TC, that's 40% fewer TC calls — net cost savings. The risk is dropping valid signals. Is a 30-conviction cutoff the right threshold, or should we start at 20 to be more permissive during the learning period?

*Recommendation: Start at 20 (very permissive) for Phase 1. Raise to 30 after 50 expert-graded trades establish calibration.*

**Q3 — Expert pre-seeding:**  
MacroExpert can be initialized with historical FRED data from day one (it's an API pull, not learned data). CryptoExpert starts with no historical calibration. SportsExpert will need historical matchup data to be useful.

*Recommendation: MacroExpert starts immediately with live API data (no pre-seeding needed — FRED data is current). CryptoExpert starts fresh; expert memory builds from live trade outcomes. SportsExpert requires a one-time historical pull of recent MLB/ATP results before going live.*

**Q4 — Sports data — ATP feed:**  
ATP official API is not publicly documented and likely requires a commercial agreement. Alternatives: (a) scrape `atptour.com` (fragile, ToS risk), (b) use a third-party sports data provider (SportsRadar ~$500/mo, Sportradar has a free trial), (c) MLB + NBA only for Phase 4, revisit ATP separately.

*Recommendation: Phase 4 = MLB + NBA only. ATP is a Phase 5 discussion after evaluating whether ACE/GHOST/PHOENIX are worth the sports data investment (currently mixed results).*

---

## 13. What This Is Not

- **Not a new TC call layer.** Experts do not invoke TC. They compute a view from APIs and return conviction + context. TC is called once per signal at the gate, same as today.
- **Not a real-time trading system.** Experts write views every 60s–5min. They are not tick-level market makers.
- **Not a replacement for SAGE.** SAGE grades intra-Kalshi patterns (which series/bucket/class combos win). Experts grade external domain context. They complement each other.
- **Not built yet.** This is a spec. Implementation begins Phase 1.
