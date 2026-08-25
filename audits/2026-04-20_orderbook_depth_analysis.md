# Order Book Depth Integration Analysis
**Status:** Read-only spec — no code changes  
**Date:** 2026-04-18  
**Context:** David's manual observation that Kalshi ask price ≠ clearing price; resting walls at depth are both execution targets and information signals. This doc scopes integrating that intelligence into the engine.

---

## Task 1 — Current Kalshi API Capabilities

### What the Engine Currently Sees

The `ticker` WebSocket channel provides per-message:

| Field | Available in WS? | Stored in `MarketData`? |
|-------|-----------------|------------------------|
| `yes_price_dollars` (mid-market) | YES | YES (`yes_price`) |
| `yes_bid` (best bid, cents) | YES, in raw msg | **NO — discarded** |
| `yes_ask` (best ask, cents) | YES, in raw msg | **NO — discarded** |
| `no_bid` (best NO bid, cents) | YES | YES (`no_bid`) |
| `no_ask` | YES, in raw msg | **NO — discarded** |
| `volume` (total contracts traded) | YES | YES (`volume_dollars`) |
| `last_price` (last trade price) | YES, in raw msg | **NO — discarded** |
| Full book depth (all levels) | **NO** — ticker channel is top-of-book only | N/A |

**Critical immediate finding:** `yes_bid` and `yes_ask` are transmitted on every WS tick but `_parse_ticker_msg()` only uses them as a fallback for computing mid-price. They are never stored in `MarketData`. The spread is structurally available at no cost — it's being discarded.

**What `spread` in `MarketData` currently means:** The `spread` field exists in the dataclass but is populated by the scan engine heartbeat (`scan_engine.py`), not from WS ticks. Its value may lag or be approximate depending on how the heartbeat derives it.

### What Kalshi Exposes Beyond the Ticker Channel

**REST Endpoints (not yet in codebase):**

| Endpoint | What it returns | Rate limit concern |
|----------|----------------|-------------------|
| `GET /markets/{ticker}/orderbook` | Full depth: all bid/ask levels with size | HIGH — 1000+ markets is infeasible to poll |
| `GET /markets/{ticker}/trades` | Recent trade history with size + aggressor | MODERATE — useful for tape reading |
| `GET /portfolio/orders/{order_id}` | Order fill status | Already implemented (`get_order_status`) |

**WebSocket `orderbook_delta` channel (not yet subscribed):**

Kalshi WS v2 supports a second subscription channel: `orderbook_delta`. Unlike `ticker` (which broadcasts single mid-price ticks), `orderbook_delta` streams incremental book updates:
- Initial snapshot on subscribe: all levels (price + size)
- Subsequent messages: deltas (level added, removed, or quantity changed)

This is the correct mechanism for real-time book depth. It requires a **second channel subscription** alongside the existing `ticker` subscription.

**Key constraint:** Kalshi allows batch subscriptions of 200 tickers per subscribe message. Subscribing to `orderbook_delta` for 1000+ markets would require 5+ subscribe messages and maintaining book state for every active market in memory.

### Rate Limits

| Method | Limit | Impact |
|--------|-------|--------|
| REST polling (`/markets/{ticker}/orderbook`) | ~10 requests/second (confirmed behavior) | 1000 markets = 100+ seconds per cycle. **Infeasible for continuous monitoring.** |
| WS `ticker` channel | No explicit rate limit — push-based | Current approach, working |
| WS `orderbook_delta` channel | No explicit rate limit — push-based | Correct approach for depth |
| REST order management | ~10 requests/second | Adequate for trade execution |

**Conclusion:** Full order book depth requires the WS `orderbook_delta` channel subscription. REST polling for books on 1000 markets is architecturally impossible.

### What Kalshi "Market Price" Actually Is

From the WS parser: `yes_price_dollars` is Kalshi's reported mid-market. The actual clearing price (last trade) is in `last_price` — also available in the WS message but currently discarded. David's manual observation captures this exactly:

- `yes_ask`: the price a market buy order would pay (taker)
- `yes_bid`: the price a market sell order would receive
- `last_price`: where the most recent trade actually executed
- `yes_price_dollars` (mid): midpoint between bid and ask

When David sees "25¢ ask, last trade 21¢," that gap (4¢) is the immediate saving from not hitting the ask.

### Current Codebase Gap Summary

| Gap | Severity | Fix effort |
|-----|----------|-----------|
| `yes_bid`, `yes_ask`, `last_price` discarded from WS msg | HIGH | Low — add 3 fields to `_parse_ticker_msg` + `MarketData` |
| No `orderbook_delta` WS subscription | HIGH | Medium — new subscription + book state manager |
| No `GET /markets/{ticker}/orderbook` REST call | MEDIUM | Low — single function in `kalshi_rest.py` |
| No last-N trades endpoint integration | LOW | Low — `GET /markets/{ticker}/trades` |

---

## Task 2 — Book-Reading Logic Specification

### Input Schema

```python
@dataclass
class BookSnapshot:
    ticker: str
    captured_at: float          # time.time()
    
    # Top-of-book (available now from WS ticker)
    yes_bid: float              # best bid (decimal, 0–1)
    yes_ask: float              # best ask (decimal, 0–1)
    last_price: float           # last traded price (decimal, 0–1)
    
    # Full depth (requires orderbook_delta subscription)
    yes_bids: list[tuple[float, int]]   # [(price, size), ...] sorted descending
    yes_asks: list[tuple[float, int]]   # [(price, size), ...] sorted ascending
    
    # Derived in book state manager from orderbook_delta + trade stream
    last_n_trades: list[tuple[float, int, str]]  # [(price, size, "buy"|"sell"), ...]
```

### Output: `BookReading` — what agents consume

```python
@dataclass
class BookReading:
    ticker: str
    
    # Execution inputs
    spread_cents: float             # ask - bid in cents
    mid_price: float                # (bid + ask) / 2
    last_price: float               # actual clearing price
    price_gap_cents: float          # ask - last_price in cents (David's key metric)
    
    # Wall identification (within 20% of last_price)
    support_wall_price: float       # largest resting bid level nearby
    support_wall_size: int          # contracts at that level
    resistance_wall_price: float    # largest resting ask level nearby
    resistance_wall_size: int
    
    # Book imbalance
    bid_ask_size_ratio: float       # total bid contracts / total ask contracts (near levels)
    bid_pressure: str               # "STRONG_BID" | "BALANCED" | "STRONG_ASK"
    
    # Thin zone identification
    thin_bid_zone: tuple[float, float] | None   # (low_price, high_price) with <100 contracts
    thin_ask_zone: tuple[float, float] | None
    
    # Fair value estimate
    fair_value_estimate: float      # weighted midpoint accounting for wall positions
    
    # Optimal limit prices (the trading signal)
    optimal_limit_entry_yes: float  # for buying YES
    optimal_limit_entry_no: float   # for buying NO (= 1 - optimal_limit_entry_yes)
    optimal_exit_yes: float         # for exiting a YES position
    
    # Metadata
    wall_is_signal: bool            # True if wall size > SIGNAL_THRESHOLD (5000+ contracts)
    confidence: str                 # "HIGH" | "MEDIUM" | "LOW" based on data freshness
    data_age_seconds: float
```

### Core Algorithms

#### 1. Wall Detection

```
WALL_THRESHOLD = 1000   # contracts — minimum size to qualify as a "wall"
SIGNAL_THRESHOLD = 5000 # contracts — size that constitutes an institutional signal
PROXIMITY_PCT = 0.20    # wall must be within 20% of last_price to be relevant

support_wall = max(
    bids where price >= last_price * (1 - PROXIMITY_PCT),
    key=lambda level: level.size
)
resistance_wall = max(
    asks where price <= last_price * (1 + PROXIMITY_PCT),
    key=lambda level: level.size
)
```

A wall qualifies as a signal when `size >= SIGNAL_THRESHOLD`. David's example (5000 contracts at 18¢) is a signal: it tells you institutional money thinks 18¢ is undervalued.

#### 2. Optimal Limit Entry Price

Four ladder levels, configurable per agent:

```
LEVEL_1 (aggressive maker): ask - 1¢
  → Sits just below the ask; fills on first downtick. Fast fill, near-taker in behavior.
  
LEVEL_2 (patient maker): bid + 1¢
  → Joins the queue one tick above best bid. Fills if sellers come in.
  
LEVEL_3 (wall front-runner): support_wall_price + 1¢
  → Sits just ahead of the wall. Fills before the wall in any downward sweep.
    David's manual approach: 19¢ when wall is at 18¢ and ask is at 25¢.
    
LEVEL_4 (behind-wall): support_wall_price - 1¢
  → Waits below the wall — only fills on flush or wall removal. Rarely used.
```

#### 3. Fair Value Estimate

```
# Weight mid-price toward the larger wall side
bid_weight = bid_wall_size / (bid_wall_size + ask_wall_size + 1)
ask_weight = ask_wall_size / (bid_wall_size + ask_wall_size + 1)

fair_value = (mid_price * 0.4) + (bid_wall_price * bid_weight * 0.3) + (ask_wall_price * ask_weight * 0.3)
```

This is a heuristic, not a quant model. It biases the estimate toward where large orders sit, which tends to be where informed capital is positioned.

#### 4. Book Imbalance Signal

```
# Total contracts in proximity zone
total_bids = sum(size for price, size in yes_bids where price >= last_price * 0.90)
total_asks = sum(size for price, size in yes_asks where price <= last_price * 1.10)

ratio = total_bids / (total_asks + 1)

if ratio > 2.0:   bid_pressure = "STRONG_BID"     # buyers dominating → bullish
elif ratio < 0.5: bid_pressure = "STRONG_ASK"     # sellers dominating → bearish
else:             bid_pressure = "BALANCED"
```

Imbalance ratio correlates with near-term price direction — not perfectly, but meaningfully.

#### 5. Thin Zone Identification

```
# A "thin zone" is a price range with fewer than THIN_THRESHOLD contracts resting
THIN_THRESHOLD = 100   # contracts

# Scan from current price upward for asks
thin_ask_low = thin_ask_high = None
for price, size in sorted_asks:
    if size < THIN_THRESHOLD:
        thin_ask_low = price
        # Continue until we hit a wall again
        ...
```

Thin zones are where price moves fast — the path of least resistance. If asks are thin from 30¢ to 45¢, a buyer pushing the price moves from 30¢ to 45¢ with minimal friction.

---

## Task 3 — Agent Integration Points

### Principle: Book as filter, not primary signal

Agents generate signals from their pattern logic (existing code). Book reading is applied **after** the internal pattern check passes, **before** `submit_signal()`. The book either:
1. Confirms the signal and improves the limit price
2. Adds context (wall size, imbalance) as signal metadata for TC
3. Vetoes the signal if book contradicts the directional thesis

This mirrors how the Expert Advisor layer works — agents detect patterns, external context validates.

### AXIOM

**Current:** Sees mid-price, emits signal at market entry price.

**With book:**
- Pre-signal check: `book.bid_pressure` — if `STRONG_ASK` and AXIOM is buying YES, lower conviction
- Entry price: LEVEL_3 (front-run support wall) for HIGH_CONVICTION; LEVEL_2 (patient maker) for GLITCH tier
- Size modulation: if `support_wall_size >= 5000`, size up (wall backstops the position); if no wall within 20%, size down to minimum
- Signal metadata added: `"book_wall_size": 5000, "book_support": 0.18, "book_imbalance": "STRONG_BID"`

**Expected effect:** Fewer market-order taker fills; lower average entry price; TC sees book confirmation in signal.

### CIPHER

**Current:** Pattern match emits signal, aggressive limit entry at price + 3¢.

**With book:**
- Pre-signal check: if book shows `STRONG_ASK` (sellers dominating) AND CIPHER's pattern is bullish YES → hard skip (book contradicts thesis)
- Entry price: LEVEL_2 or LEVEL_3 depending on wall proximity
- **Key integration:** CIPHER's thesis is pattern-based (velocity, level break). If the book is stacked with asks exactly at the pattern level, the breakout is fake or needs more size to execute → skip

**Note:** CIPHER's structural loss problem (0.20x win/loss ratio) is not fixed by book reading. But skipping signals where book contradicts thesis should reduce the tail losses.

### DELTA

**Current:** Arb gap detection on BTC ladder. Enters when gap ≥ threshold.

**With book:**
- Pre-signal check: `book.spread_cents` — if spread > arb gap size, no trade is viable. Gap must exceed `spread_cents + 2` to be real after execution costs.
- Entry price: LEVEL_3 (front-run wall) — DELTA holds 40+ minutes, missing entry by 30s is fine
- Key signal: thin zone above entry = price can move to target with low friction = higher confidence

**DELTA-specific rule:**
```
if arb_gap_pct * entry_price < spread_cents / 100:
    logger.info("[DELTA] Arb gap %.1f%% smaller than spread %.1f¢ — skip", arb_gap_pct*100, spread_cents)
    return
```
This single check would have eliminated several DELTA trades that appeared profitable on mid-price but were impossible to execute profitably.

### DIAMOND

**Current:** Generates 381 signals; TC converts ~1%. Book context in the signal may dramatically improve TC conversion by giving TC concrete evidence.

**With book:**
- Signal metadata: include full `BookReading` summary in TC prompt
- TC can verify: "book shows 4000-contract support at 22¢, ask at 28¢, last trade 24¢ — LEVEL_3 entry at 23¢ is well-positioned"
- TC conversion hypothesis: DIAMOND's signals are currently abstract (agent says "edge=25%"). Book data makes them concrete and verifiable.

### ORACLE (future)

Political contracts are thin, slow-moving markets. Book reading for ORACLE:
- Spread may be very wide (10–20¢) — any entry must account for this
- Wall detection: if one side has 0 contracts resting, DO NOT enter on that side (illiquid)
- Fair value estimate is especially useful here — if the market has 5¢ bid and 30¢ ask, fair value might be 12¢, which is where TC should price the limit

---

## Task 4 — Execution Price Strategy

### The Limit Pricing Ladder

| Level | Formula | When to use | Agents |
|-------|---------|-------------|--------|
| **MARKET** | `ask + 5¢` (current aggressive limit) | Stop-losses, urgent exits, settlement approaching | All (stop-loss path only) |
| **L1 Aggressive Maker** | `ask - 1¢` | High conviction + thin asks above | BLITZ, CIPHER (velocity) |
| **L2 Patient Maker** | `bid + 1¢` | Standard resting limit, balanced book | AXIOM (GLITCH tier), TIDE |
| **L3 Wall Front-Runner** | `wall_bid + 1¢` | Wall identified, size confirms | AXIOM (HIGH_CONVICTION), DELTA, DIAMOND |
| **L4 Behind-Wall** | `wall_bid - 1¢` | Extreme patience, large walls | ORACLE (political, slow markets) |

### Conviction Tier → Limit Level Mapping (proposed)

| Conviction Tier | Default Entry Level | Override condition |
|----------------|---------------------|-------------------|
| PROPHECY | L1 (aggressive) | No override — at PROPHECY, speed matters |
| HIGH_CONVICTION | L3 if wall exists, L2 if not | Wall size < 500 → fall to L2 |
| GLITCH | L2 | No wall condition |

**Rationale:** PROPHECY signals have strong edge — the 1¢ fee savings from resting at L2 is small relative to the risk of missing the entry. L1 (aggressive maker, 1¢ below ask) is a reasonable compromise — faster than a true resting maker but still potentially 0% fee if an existing sell rests at ask-1¢.

### Exit Pricing

| Scenario | Strategy | Rationale |
|----------|---------|-----------|
| Target exit (winning) | Limit at `resistance_wall_price - 1¢` | Exits just ahead of resistance, fills before the wall kills the order |
| Target exit (no book data) | Limit at target_price | Current approach |
| Stop-loss | **Always market** (`bid - 5¢`) | Guarantee exit, accept fee. Non-negotiable. |
| TC exit review → EXIT verdict | Market if hold_seconds > hold_threshold, otherwise L1 | Urgency depends on how long we've held |
| Settlement < 5 min | **Always market** | No time for resting limit, settlement risk too high |

### Trailing Exit (advanced, Phase 3+)

As price moves favorably, cancel the resting exit and re-post 1¢ below new resistance:
1. Position enters at 22¢, target at 35¢
2. Price moves to 30¢, new resistance wall appears at 32¢
3. Cancel exit at 35¢, re-post at 31¢ (ahead of the 32¢ wall)
4. Exit fills at 31¢ instead of waiting for 35¢ that may never come

**Complexity:** Each cancel + re-post is an API call. At 10 req/s limit, trailing at every tick is infeasible. Implement trailing only on position check intervals (60s), not on every tick.

### Fallback Rules (hard constraints)

```python
# In order_manager.py — these override ALL limit pricing decisions

if position.days_to_settlement < (5/1440):   # < 5 minutes
    → ALWAYS market order
    
if exit_reason == "stop_loss":
    → ALWAYS market order
    
if limit_pending_seconds > LIMIT_TIMEOUT:
    → CANCEL limit + market fallback (DELTA: 60s, AXIOM: 30s, DIAMOND: 120s)
    
if partial_fill_ratio < 0.6 and not timeout:
    → WAIT (do not force market for partial)
```

---

## Task 5 — Position State Machine

This is the hardest part. The current engine has two implicit states: `PENDING` (written to `state.pending`) and `OPEN` (written to `state.positions`). There is no tracking of limit order lifecycle.

### Proposed State Machine

```
                    submit_signal()
                         │
                         ▼
                   [PENDING_ENTRY]
                 limit order posted
                    order_id stored
                         │
              ┌──────────┴──────────┐
          fill confirmed        timeout / signal stale
              │                      │
              ▼                      ▼
           [OPEN]               [CANCELLED]
        position real           order cancelled
        entry_price locked      no DB row written
              │
    ┌─────────┴──────────┐
limit exit posted    stop-loss triggered
    │                      │
    ▼                      ▼
[PENDING_EXIT]          market sell
limit resting           immediately
    │
┌───┴───┐
fill  re-post/cancel
 │
 ▼
[CLOSED]
DB row written
fees computed
```

### State Transitions — Detailed Rules

| From → To | Trigger | Action |
|-----------|---------|--------|
| `PENDING_ENTRY → OPEN` | `get_order_status(order_id)` returns `filled` | Record actual fill price, quantity; create Position |
| `PENDING_ENTRY → OPEN` (partial) | Partial fill detected (`filled_count < count`) | Create Position with `quantity = filled_count`; cancel remainder |
| `PENDING_ENTRY → CANCELLED` | Timeout elapsed OR signal price moved > threshold | `cancel_order(order_id)`; discard |
| `OPEN → PENDING_EXIT` | Exit condition met (stop, target, TC review, max hold) | Post limit sell; record exit order_id |
| `OPEN → CLOSED` (stop-loss) | Stop-loss price hit | Market sell immediately; bypass PENDING_EXIT |
| `PENDING_EXIT → CLOSED` | Fill confirmed on exit order | Write DB row; compute fees; log outcome |
| `PENDING_EXIT → PENDING_EXIT` (re-post) | Trailing logic or better price available | `cancel_order(exit_id)`; post new limit |
| `PENDING_EXIT → CLOSED` (forced) | Settlement < 5 min OR TC forced exit | Market sell; cancel pending exit first |

### Partial Fill Handling

```python
MIN_FILL_FRACTION = 0.60   # if less than 60% fills, cancel and discard

if filled_count < requested_count:
    fill_fraction = filled_count / requested_count
    if fill_fraction >= MIN_FILL_FRACTION:
        # Accept partial — proceed with smaller position
        position.quantity = filled_count
        logger.warning("[OrderManager] Partial fill: %d/%d on %s", filled_count, requested_count, ticker)
    else:
        # Too small — cancel remainder, discard
        cancel_order(order_id)
        logger.warning("[OrderManager] Partial fill too small (%d/%d) — discarding", filled_count, requested_count)
        return None
```

### Data Additions to `Position` Dataclass

```python
@dataclass
class Position:
    # ... existing fields ...
    
    # New fields for limit order state machine
    state: str = "OPEN"                     # "PENDING_ENTRY" | "OPEN" | "PENDING_EXIT" | "CLOSED" | "CANCELLED"
    entry_order_id: str = ""                # limit entry order ID (for status polling)
    exit_order_id: str = ""                 # limit exit order ID (for status polling)
    entry_limit_price: float = 0.0          # the limit price posted (may differ from fill price)
    entry_posted_at: float = 0.0            # time.time() when limit was posted
    entry_timeout_seconds: float = 60.0     # per-agent timeout
    book_support_at_entry: float = 0.0      # support wall price recorded at entry
    book_wall_size_at_entry: int = 0        # wall contracts recorded at entry
```

### Order Status Polling Architecture

The heartbeat loop in `scan_engine.py` (every 60s) is the natural home for limit order status checks:

```python
# In scalper_engine or main.py heartbeat
def _check_pending_orders():
    for ticker, position in state.get_pending_entry_positions():
        status = kalshi_rest.get_order_status(position.entry_order_id)
        filled = status.get("filled_count", 0)
        
        if filled >= position.requested_quantity * MIN_FILL_FRACTION:
            _confirm_entry(position, filled)
        elif time.time() - position.entry_posted_at > position.entry_timeout_seconds:
            _cancel_and_discard(position)
```

**Important:** Do NOT call `get_order_status` on every WS tick — that's 1000+ REST calls per second. Poll on the existing 60s heartbeat cycle.

---

## Task 6 — Paper Mode Simulation

### The Problem

Current paper mode: instant fill at exactly `entry_price`. This is unrealistic for limit orders — it overstates fill rate and understates timing uncertainty.

### Option A — Probabilistic Fill Model (recommended for now)

Using the fill rate data from Task 3 (fee analysis):

```python
LIMIT_FILL_PROB = {
    "BTC_LADDER_LEVEL2": 0.65,    # patient maker at bid+1¢
    "BTC_LADDER_LEVEL3": 0.70,    # wall front-runner
    "PGA_GOLF_LEVEL2":   0.85,
    "PGA_GOLF_LEVEL3":   0.90,
    "TENNIS_LEVEL2":     0.75,
}

# In paper mode place_order():
if order_type == "passive_limit":
    contract_type = _classify_ticker(ticker)
    fill_prob = LIMIT_FILL_PROB.get(f"{contract_type}_{level}", 0.70)
    if random.random() > fill_prob:
        logger.info("[PAPER] Simulated non-fill: %s limit at %.2f (prob=%.0f%%)", ticker, price, fill_prob*100)
        return None   # position never created
    else:
        # Simulate fill at a slightly different price (0–2¢ slippage)
        fill_price = price + random.uniform(-0.01, 0.01)
        ...
```

**Limitation:** Random fill is a rough simulation. It introduces noise but no bias — over many trades, the statistics will approximate real fill rates.

### Option B — Live small-size testing (recommended for validation)

Paper mode with probabilistic fill gives a statistical approximation. But the true fill rate, fill timing, and partial fill behavior can only be validated with real orders. Recommendation:

**Before deploying limit orders at QUALIFIED sizing, run a 10-trade live validation sprint:**
- 10 trades, 1 contract each (minimum position)
- Mix of L2 and L3 limit entries across BTC_LADDER and PGA_GOLF
- Record: time-to-fill, fill fraction, actual fill price vs limit price
- Compare against paper mode predictions

This costs ~$50 in entry prices and is the only reliable calibration method.

### Option C — Keep paper mode as-is, accept the gap

Paper mode continues to simulate market fills. The paper P&L numbers are optimistic (no fill risk, no slippage). Limit order strategy is implemented only in live mode. The trade-off: we don't know paper mode's predictive accuracy for limit orders until we go live.

**David's call:** Option A adds realism to paper mode; Option B gives real data faster.

---

## Task 7 — Risk Flags

### Risk 1: Capital Locked in Indefinite Limit Orders

**Scenario:** Five agents each post a limit entry order. None fill within their timeout windows. Five cancellations fire. But between posting and cancelling, no other signals can use that capital allocation (exposure manager blocks duplicate positions on the same ticker).

**Quantified exposure:** At PROPHECY sizing ($25), 5 pending limits = $125 locked for 30–120 seconds. At HIGH_CONVICTION ($100), = $500 locked.

**Mitigation:** Separate pending-entry capital from open-position capital in the exposure manager. Pending limits count as "soft reservation" at 50% weight, not full blocking. If a better signal arrives on the same ticker, cancel the pending limit and replace.

**Risk level: MEDIUM** — manageable with exposure manager changes.

---

### Risk 2: Market Moves Against Us Faster Than Cancel + Re-post

**Scenario:** DELTA posts NO entry at 24¢. BTC drops and the NO contract runs to 35¢ in 20 seconds. DELTA's limit at 24¢ fills — but it's now a losing position from the start (filled into a moving market).

**This is actually the opposite risk from "missing the entry"** — here the limit fills at a price that's now stale. For a NO contract: if you intended to buy NO at 24¢ because NO was cheap, and by the time you fill it's already at 35¢ (your 24¢ is even cheaper than expected), this could still be a win.

**The real risk:** For aggressive momentum signals (BLITZ, CIPHER), a limit at L2 may fill into a reversal — the taker who hit your bid was an informed seller unloading into your order. You buy right as the market turns.

**Mitigation:** For BLITZ/CIPHER, keep market (aggressive limit) orders. Reserve resting limits for DELTA/DIAMOND/ORACLE where the hold is longer and the signal is mean-reversion or arb, not momentum.

**Risk level: HIGH for momentum agents (don't use resting limits on BLITZ/CIPHER), LOW for DELTA/DIAMOND.**

---

### Risk 3: Partial Fills + Same Fee Overhead

**Scenario:** DIAMOND posts 40-contract limit. 22 fill (55%). MIN_FILL_FRACTION = 0.60 → position discarded. Entry fee on 22 contracts was already debited. Then we re-post and another 20 fill.

**Wait — Kalshi fees:** Maker limit orders that fill pay 0% fee. If the partial fill is maker: no fee on unfilled portion, no fee on the partial. **Partial fills are fee-free** for maker orders. The risk is operational (partial tracking complexity), not fee-based.

**Risk level: LOW for fees, MEDIUM for position tracking.**

---

### Risk 4: Book Spoofing — Fake Walls

**Scenario:** A large participant posts 10,000 contracts at 18¢ NO (bid). Our system reads this as "strong support at 18¢." We post at 19¢. The 18¢ wall gets pulled before any sellers fill us. We're now resting in a vacuum at 19¢ while the market moves to 12¢.

**Reality check on Kalshi:** Kalshi prediction markets attract retail flow and some institutional players. True spoofing (posting large walls to manipulate and then pulling) does exist in equity markets. On Kalshi binary contracts:
- Markets are smaller — 10,000 contracts at 18¢ = $1,800 notional. Spoofing this is cheap.
- Counterbalance: the market settles to 0 or 1, so persistent fake walls get arbitraged away.
- **Practical risk:** Resting walls on close-to-settlement contracts are more likely genuine than on long-dated ones.

**Mitigation:**
- Require wall persistence: only classify a wall as a signal if it's been present for 60+ seconds (wall detection needs history, not just snapshot)
- Size relative check: flag if wall appeared within 5 seconds of our signal (freshly placed = suspicious)
- Only use wall-front-running (L3) on contracts with multiple days to settlement — avoid it near expiry

**Risk level: LOW-MEDIUM.** Kalshi markets are too small for systematic spoofing to be common. Treat walls as informational but not infallible.

---

### Risk 5: WS `orderbook_delta` Memory Footprint

Maintaining full book state for 1000+ markets in memory:
- Each market: ~20 levels × 2 sides × (price: 4 bytes + size: 4 bytes) = ~320 bytes/market
- 1000 markets: ~320KB — trivially small
- With `orderbook_delta` processing overhead and Python dict overhead: ~5MB realistic estimate

**Risk level: NEGLIGIBLE.**

---

### Risk 6: Missing Settlement Detection

If a contract settles while a pending limit entry is live, Kalshi will reject the fill or return an error. The engine must handle this gracefully.

**Detection:** `days_to_settlement < 0.001` (< ~1.5 minutes) → cancel all pending entries for that ticker.

**Risk level: LOW** — the existing max_hold and HTSR logic already handles near-settlement exits. The cancellation case just needs to be added.

---

## Task 8 — Implementation Scoping

### Component Breakdown

**Module 1: Top-of-book fields in WS parser (IMMEDIATE, Phase 0)**

- **File:** `connectors/kalshi_ws.py` — `_parse_ticker_msg()`
- **Change:** Extract and return `yes_bid`, `yes_ask`, `last_price` from the raw WS message
- **File:** `core/shared_state.py` — add fields to `MarketData`: `yes_bid`, `yes_ask`, `last_price`
- **Effort:** ~40 lines of code changes
- **Risk:** LOW — additive only, no logic changes
- **Value:** Immediate. Spread and ask/bid gap are available on every tick at zero infrastructure cost. Agents can use `market.yes_ask - market.last_price` without any new subscriptions.

---

**Module 2: REST orderbook endpoint (LOW PRIORITY)**

- **File:** `connectors/kalshi_rest.py` — add `get_orderbook(ticker)`
- **Endpoint:** `GET /markets/{ticker}/orderbook`
- **Use case:** On-demand book snapshot for TC's decision (TC could call this for DIAMOND signals)
- **Effort:** ~20 lines
- **Risk:** LOW
- **Value:** MEDIUM — on-demand is fine for TC-assisted signals; not suitable for continuous agent use

---

**Module 3: WS `orderbook_delta` subscription + book state (PHASE 2)**

- **File:** `connectors/kalshi_ws.py` — new channel subscription alongside `ticker`
- **New file:** `connectors/book_state.py` — maintains full book per ticker from delta stream
- **New file:** `connectors/book_reader.py` — computes `BookReading` from `BookSnapshot`
- **Effort:** ~300 lines
- **Risk:** MEDIUM — new subscription type, delta state management has edge cases (sequence gaps require full snapshot re-fetch)
- **Value:** HIGH — unlocks full depth, wall detection, thin zone, imbalance

**This is the prerequisite for Task 2's `BookReading` signals.**

---

**Module 4: Position state machine rebuild (PHASE 2 — critical path)**

- **Files:** `core/shared_state.py` (Position dataclass), `scalper/order_manager.py` (state transitions), `scalper/scalper_engine.py` (heartbeat polling loop)
- **Effort:** ~400 lines new + ~200 lines changed
- **Risk:** **HIGH** — this touches the core execution path. Paper mode behavior changes. Any bug here can cause double-entries, missed exits, or orphaned pending orders.
- **Required before any resting limit orders go live.**
- **Testing required:** Full integration test with paper mode limit order cycle before live deployment.

---

**Module 5: Agent book integration (PHASE 3)**

- **Files:** Each agent's `evaluate()` method
- **Change:** Add `book = book_reader.get_reading(market.ticker)` before `submit_signal()`; use to filter and annotate
- **Effort:** ~50 lines per agent × 4 primary agents = ~200 lines
- **Risk:** LOW per agent — each change is isolated

---

**Module 6: Paper mode fill simulation (PHASE 2, alongside state machine)**

- **File:** `scalper/order_manager.py`
- **Change:** Add `LIMIT_FILL_PROB` lookup and random fill simulation in paper mode
- **Effort:** ~50 lines
- **Risk:** LOW — paper mode doesn't affect live trading

---

### Implementation Sequencing

```
Phase 0 (now, before any other work):
  ├── Top-of-book fields in WS parser (40 lines, low risk)
  └── Agents can immediately use spread + ask/bid gap as weak signal

Phase 1 (alongside Expert Advisor build):
  ├── REST get_orderbook() endpoint (20 lines)
  └── TC can request book on DIAMOND/ORACLE signals manually

Phase 2 (dedicated sprint, before live mode):
  ├── WS orderbook_delta subscription + book_state.py (300 lines)
  ├── Position state machine rebuild (600 lines, HIGH risk)
  └── Paper mode fill simulation (50 lines)

Phase 3 (after state machine is validated):
  ├── BookReading computation (200 lines)
  ├── Agent integration (200 lines)
  └── Limit pricing ladder per agent (100 lines)

Live mode validation sprint:
  └── 10 trades × 1 contract each, L2/L3 limits only, record fill data
```

### Time Estimate Caveat

Not providing time estimates per spec. The ordering above reflects dependency chains and risk levels, not schedule. The state machine (Phase 2) is the gating item — everything else can proceed or wait independently.

---

## Summary

| Finding | Impact |
|---------|--------|
| `yes_bid`, `yes_ask`, `last_price` already in WS msg — being discarded | Immediate fix, zero new infrastructure |
| Full book depth requires `orderbook_delta` WS channel — not subscribed | Phase 2 work |
| REST polling for 1000+ books is infeasible | WS is the only viable path |
| Position state machine is the hardest dependency | Gating item for all resting limits |
| Book spoofing is real but low probability on Kalshi | Mitigate with wall persistence check, not by avoiding book reading |
| DELTA and DIAMOND benefit most from book-assisted entries | DELTA's arb gap check vs spread is a near-immediate improvement |
| Resting limits are wrong for momentum agents (BLITZ, CIPHER) | Keep market entry for velocity-dependent signals |
| Phase 0 unlocks spread-aware agent logic immediately | David should approve this regardless of full book decision |

**The minimum viable path to order book awareness:**
1. Extract `yes_bid`, `yes_ask`, `last_price` from the existing WS parser (40 lines)
2. Store in `MarketData`
3. DELTA: add `if arb_gap < spread: skip` check (5 lines)

That alone, with no new infrastructure, eliminates a meaningful subset of DELTA's bad entries and gives every agent access to spread data for the first time.
