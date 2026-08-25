# Phase 1 — Book Scanner Implementation Spec
**Date:** 2026-04-18  
**Status:** Spec only — no implementation. David reviews before build.  
**Prerequisite:** Agent bench active (AXIOM, SAGE, ECHO only)  
**Companion docs:** `2026-04-20_orderbook_depth_analysis.md`, `2026-04-20_fee_optimization_analysis.md`

---

## Scope

Phase 1 goal: give AXIOM access to real-time bid/ask spread and wall detection for every contract it evaluates. This informs entry price selection and eliminates entries where the arb gap is smaller than the spread.

Phase 1 does **not** include:
- `orderbook_delta` WebSocket streaming (Phase 2)
- Full position state machine (Phase 2)
- Limit order placement in live mode (Phase 2)
- Per-agent integration beyond AXIOM (Phase 2)

---

## Task 1 — `connectors/kalshi_book.py` — File Location and Function Signatures

**File:** `connectors/kalshi_book.py`  
**Pattern:** Follows `kalshi_rest.py` — uses existing `_get()`, `_limiter`, `_get_signed_headers()`. Import from `connectors.kalshi_rest` directly; no new auth/signing code.

### Dataclasses

```python
from dataclasses import dataclass, field
from typing import Optional

WALL_THRESHOLD = 1000   # contracts — resting size qualifies as a wall
SIGNAL_THRESHOLD = 5000 # contracts — institutional-level wall

@dataclass
class BookLevel:
    price_cents: int    # 1–99
    size: int           # contracts resting at this level

@dataclass
class BookSnapshot:
    ticker: str
    fetched_at: float                      # time.time() at fetch
    yes_bids: list[BookLevel]              # sorted desc (best bid first)
    yes_asks: list[BookLevel]              # sorted asc (best ask first)
    best_bid: Optional[BookLevel]          # None if book empty
    best_ask: Optional[BookLevel]          # None if book empty
    spread_cents: Optional[int]            # best_ask.price - best_bid.price
    wall_bid: Optional[BookLevel]          # largest resting bid ≥ WALL_THRESHOLD
    wall_ask: Optional[BookLevel]          # largest resting ask ≥ WALL_THRESHOLD
    bid_depth_10pct: int                   # total contracts within 10% of mid
    ask_depth_10pct: int                   # total contracts within 10% of mid
    fair_value_cents: Optional[float]      # size-weighted mid across top 5 levels
    stale: bool = False                    # True if fetch failed / data >120s old
```

### Public Functions

```python
def fetch_book(ticker: str) -> Optional[BookSnapshot]:
    """
    Call GET /trade-api/v2/markets/{ticker}/orderbook.
    Parse response into BookSnapshot.
    Returns None on API error or empty response.
    Uses existing _limiter (30 req/sec token bucket).
    """

def analyze_book(snapshot: BookSnapshot) -> BookReading:
    """
    Derive trading signals from a BookSnapshot.
    Returns BookReading with wall info, fair value, optimal entry price.
    Pure function — no I/O.
    """

def get_book_reading(ticker: str) -> Optional[BookReading]:
    """
    Convenience: fetch_book() + analyze_book() in one call.
    Returns None if fetch fails.
    """

def poll_books(tickers: list[str], interval_seconds: int = 60) -> None:
    """
    Background daemon thread loop.
    For each ticker in list: call fetch_book(), analyze_book(), write to data/book/.
    Respects _limiter. Writes _latest.json aggregate after each full cycle.
    Intended to run as a daemon thread from main.py.
    """

def get_cached_reading(ticker: str) -> Optional[BookReading]:
    """
    Return the most recent BookReading from data/book/{ticker}.json (in-process cache).
    Returns None if no data or data is stale (age > 120s).
    Zero API calls — safe to call from evaluate() hot path.
    """
```

### `BookReading` (output of `analyze_book`)

```python
@dataclass
class BookReading:
    ticker: str
    timestamp: float
    support_wall: Optional[BookLevel]      # strongest bid wall
    resistance_wall: Optional[BookLevel]   # strongest ask wall
    bid_ask_size_ratio: Optional[float]    # bid_depth_10pct / ask_depth_10pct
    fair_value_cents: Optional[float]      # size-weighted mid
    spread_cents: Optional[int]
    optimal_limit_entry_cents: Optional[int]  # L1/L2/L3 computed price (see pricing ladder)
    book_age_seconds: float                # seconds since fetched_at
    is_stale: bool                         # age > 120s
    signal_wall: bool                      # any side has SIGNAL_THRESHOLD wall
```

---

## Task 2 — Polling Cadence

### Tier Definitions

| Tier | Markets | Interval | Rationale |
|------|---------|----------|-----------|
| **HOT** | AXIOM-eligible (liquid, BTC/ETH/PGA series) | 60s | Price changes meaningfully; refresh before each agent eval |
| **WARM** | Other liquid markets (78 - HOT) | 300s | Background info, less urgent |
| **COLD** | Illiquid, WATCH class | Not polled | No trading interest |

In Phase 1, only HOT tier is implemented. WARM is reserved for Phase 2.

### Ticker Selection for HOT Tier

At startup, `poll_books()` receives a ticker list derived from the `heartbeat_latest.json` liquidity-passed markets filtered to AXIOM series:
- `KXBTCD*` — Bitcoin daily contracts
- `KXETHD*` — Ethereum daily contracts
- `KXPGAR*` — PGA 3-round lead
- `KXPGATOUR*` — PGA tournament contracts
- `KXPGAMATCH*` — PGA matchup contracts

Estimated HOT tier size: **30–60 markets** (varies with tournament schedule).

### Thread Design

`poll_books()` runs as a single daemon thread launched from `main.py` after the first heartbeat populates `shared_state`. Pseudocode:

```python
def poll_books(tickers: list[str], interval_seconds: int = 60) -> None:
    while True:
        cycle_start = time.monotonic()
        readings = {}
        for ticker in tickers:
            snapshot = fetch_book(ticker)
            if snapshot:
                reading = analyze_book(snapshot)
                _write_book_state(ticker, reading)
                readings[ticker] = reading
        _write_latest_aggregate(readings)
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0.0, interval_seconds - elapsed)
        time.sleep(sleep_for)
```

The `_limiter` in `kalshi_rest.py` is shared across all threads — book polling automatically yields to order placement calls.

---

## Task 3 — Data Storage

### Phase 1 — File-Based (JSON)

**No new DB table in Phase 1.** Reasons:
- Book data is ephemeral state, not trade history — disk files are sufficient
- Avoids DB schema migration on live system
- JSON files are readable by dashboard without new DB queries
- Pattern matches existing `triggers/` and `memory/` atomic-write pattern

**Directory:** `data/book/` (create if absent)

**Per-ticker file:** `data/book/KXBTCD-26APR2417-T74499.99.json`
```json
{
  "ticker": "KXBTCD-26APR2417-T74499.99",
  "timestamp": "2026-04-18T22:10:00Z",
  "fetched_at": 1713477000.0,
  "spread_cents": 3,
  "best_bid_cents": 47,
  "best_ask_cents": 50,
  "wall_bid": {"price_cents": 45, "size": 2400},
  "wall_ask": null,
  "fair_value_cents": 48.3,
  "optimal_limit_entry_cents": 46,
  "bid_ask_size_ratio": 1.4,
  "signal_wall": false,
  "book_age_seconds": 12.0,
  "is_stale": false
}
```

**Write pattern:** Atomic — write to `{ticker}.json.tmp` then `os.replace()`. Same pattern as `echo.py`.

**Aggregate file:** `data/book/_latest.json` — written at end of each poll cycle.
```json
{
  "cycle_timestamp": "2026-04-18T22:10:00Z",
  "markets_polled": 47,
  "markets_with_walls": 3,
  "markets_stale": 0,
  "readings": {
    "KXBTCD-26APR2417-T74499.99": { ...BookReading fields... },
    ...
  }
}
```

### Phase 2 (future) — DB Schema

If historical book data is needed for analysis:

```sql
CREATE TABLE book_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT    NOT NULL,
    snapshot_time    TEXT    NOT NULL,  -- ISO 8601 UTC
    best_bid_cents   INTEGER,
    best_ask_cents   INTEGER,
    spread_cents     INTEGER,
    wall_bid_cents   INTEGER,
    wall_bid_size    INTEGER,
    wall_ask_cents   INTEGER,
    wall_ask_size    INTEGER,
    bid_depth_10pct  INTEGER,
    ask_depth_10pct  INTEGER,
    fair_value_cents REAL,
    signal_wall      INTEGER DEFAULT 0, -- 0/1
    raw_json         TEXT               -- full BookSnapshot JSON
);
CREATE INDEX idx_book_ticker_time ON book_snapshots(ticker, snapshot_time);
```

**Retention policy:** Prune rows older than 7 days via a nightly cleanup cron (added when schema is added).

---

## Task 4 — Dashboard Integration

### New Endpoint: `GET /api/book`

**File:** `warroom/app.py`  
**Pattern:** Same as existing `/api/live_feed`, `/api/signals`, `/api/leaderboard` routes.

```python
@app.route("/api/book")
def api_book():
    """Return current book readings for dashboard display."""
    import json, os, time
    latest_path = os.path.join(SYNDICATE_ROOT, "data", "book", "_latest.json")
    if not os.path.exists(latest_path):
        return jsonify({"error": "no book data", "markets": {}}), 200
    with open(latest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Attach staleness flag if file itself is old
    file_age = time.time() - os.path.getmtime(latest_path)
    data["file_age_seconds"] = round(file_age, 1)
    return jsonify(data)
```

### Dashboard Display (warroom template)

New panel in the War Room UI showing:
- Per-market: spread, best bid/ask, wall presence (icon if `signal_wall=true`)
- Sorted by AXIOM activity (markets with recent AXIOM signals bubble to top)
- Auto-refresh: JS `setInterval` polls `/api/book` every 10s (same cadence as existing live feed)
- Stale indicator: market card turns grey if `book_age_seconds > 90`

Template changes: add one new panel to the existing War Room HTML template. No new template file needed.

---

## Task 5 — Rate Limit Math

### Current API Call Budget

| Source | Calls/cycle | Cycle | Calls/min |
|--------|------------|-------|-----------|
| `get_all_markets()` (heartbeat) | ~12 paginated calls | 5 min | 2.4 |
| Order placement (paper, rare) | ~2 per trade | ad hoc | ~0.1 |
| Order status checks | ~3 per trade | ad hoc | ~0.1 |
| **Current total** | — | — | **~2.6 req/min** |

### Book Poller Addition (Phase 1, HOT tier)

| Metric | Value |
|--------|-------|
| HOT tier markets | ~50 |
| Polling interval | 60s |
| Calls per cycle | 50 |
| **Calls per minute** | **50** |
| Calls per second | **0.83 req/sec** |

### Combined Budget

| Source | req/sec |
|--------|---------|
| Existing calls | 0.04 |
| Book poller (Phase 1) | 0.83 |
| **Total** | **0.87 req/sec** |
| Ceiling (Advanced tier) | 30 req/sec |
| **Headroom** | **34×** |

**Verdict: No rate limit risk at 60s interval on 50 markets.** Even aggressive 10s polling (5 req/sec) leaves 6× headroom.

### Pressure Points

1. **Tournament weekends (PGA):** Market count spikes. At 150 PGA contracts × 1/60s = 2.5 req/sec — still fine.
2. **Concurrent order flow:** During high-conviction entries, order placement + status checks add ~5 calls/trade. At 1 trade/min = 5 req/sec additional — still within ceiling.
3. **Rate limiter sharing:** `_limiter` in `kalshi_rest.py` is module-global. Book poller shares the same token bucket as order calls — book polls automatically yield when orders are being placed.

### Conservative Recommendation

Start at 60s interval. Move to 30s if analysis shows AXIOM signals fire faster than book data can refresh (check via log: `[BookPoller] Stale read: ticker=X age=62s`). Do not go below 15s without measuring actual impact.

---

## Task 6 — Implementation Sequencing

### Minimum Viable Phase 1

**Step A — Extract `yes_bid`/`yes_ask` from WS (40 lines, zero new infrastructure)**
- `connectors/kalshi_ws.py`: `_parse_ticker_msg()` — extract `yes_bid`, `yes_ask`, `last_price` already present in WS ticker message
- `core/shared_state.py`: add `yes_bid`, `yes_ask`, `last_price` fields to `MarketData`
- Benefit: every agent gets real-time spread data immediately, no new poller needed
- Risk: LOW — read-only parse change

**Step B — DELTA spread filter (5 lines)**
- `agents/delta.py`: `evaluate()` — add `if arb_gap_pct * entry_price < market.spread_cents: return None`
- Benefit: eliminates DELTA entries where fee + spread makes the trade structurally impossible to profit
- Risk: LOW — adds one guard clause

**Step C — `connectors/kalshi_book.py` REST poller (Phase 1 full build)**
- New file, background thread, `data/book/` state files
- `warroom/app.py`: add `/api/book` endpoint
- Risk: MEDIUM — new module, new thread, new file I/O

**Step D — AXIOM integration**
- `agents/axiom.py`: `evaluate()` — call `get_cached_reading(ticker)`, use wall info to select limit price
- Risk: MEDIUM — changes AXIOM entry logic (only active trading agent)

### Recommended Order

1. **Get approval from David** — this doc
2. **Step A only** — WS field extraction (immediate, no poller needed)
3. **Observe 48h** — confirm `yes_bid`/`yes_ask` in logs, agent access patterns
4. **Step B** — DELTA spread filter (only active if DELTA is re-enabled)
5. **Step C** — REST book poller (needs dedicated test session)
6. **Step D** — AXIOM integration (after book poller has 48h of clean data)

Steps A+B can be approved and implemented together — they touch different files and have no dependency on each other.

---

## Files Created/Modified Summary

| File | Change Type | Step |
|------|------------|------|
| `connectors/kalshi_ws.py` | Edit — extract `yes_bid`, `yes_ask` | A |
| `core/shared_state.py` | Edit — add fields to `MarketData` | A |
| `agents/delta.py` | Edit — spread guard clause | B |
| `connectors/kalshi_book.py` | **New file** — REST book poller | C |
| `data/book/` | New directory + JSON state files | C |
| `warroom/app.py` | Edit — add `/api/book` route | C |
| `warroom/templates/*.html` | Edit — add book panel to War Room | C |
| `agents/axiom.py` | Edit — use book reading in evaluate() | D |
| `main.py` | Edit — launch book poller thread at startup | C |

---

## Open Questions for David

1. **Step A immediate approval?** WS field extraction is 40 lines, zero risk, zero new infrastructure. Can approve independently of rest of Phase 1.

2. **60s or 30s poll interval for HOT tier?** 60s is safe. 30s is still comfortable. Your call on latency vs API budget.

3. **DB table now or file-based Phase 1 only?** Recommendation is file-based for Phase 1. Add DB table in Phase 2 when historical analysis is needed. But if you want the data from day one, add the schema now.

4. **Dashboard book panel priority?** War Room panel is useful but not gating for the trading benefit. Can be deferred to Phase 2 if you want to move faster on Step A–D.

5. **AXIOM conviction threshold for limit vs market?** The orderbook analysis proposed: `HIGH_CONVICTION → limit at L3 (wall_bid + 1¢)`, `GLITCH → market (no wait)`. Confirm before Step D implementation.
