"""
shared_state.py — Thread-safe shared memory for The Syndicate.

All agents read/write through this object. Single source of truth.
Extended from Atlas for multi-asset, multi-class architecture.

Contract classes: SCALP / SWING / POSITION / WATCH
Access patterns: acquire lock, read/write, release. Never hold lock during I/O.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


@dataclass
class MarketData:
    ticker: str
    yes_price: float            # 0.0–1.0 (e.g. 0.62)
    no_bid: float
    volume_dollars: float
    spread: float               # bid-ask spread (float)
    days_to_settlement: float   # calendar days until resolution
    contract_class: str         # SCALP / SWING / POSITION / WATCH
    series_ticker: str          # parent series (e.g. "KXBTC")
    last_update: float          # time.time()
    price_history: list = field(default_factory=list)  # list of (ts, yes_price)
    velocity: float = 0.0       # price change per minute over last velocity_window


@dataclass
class TennisGame:
    match_id: str
    player1: str
    player2: str
    score_raw: str              # raw score string from api-tennis
    set_scores: list            # list of (p1_games, p2_games) per set
    current_set: int
    current_game: str           # e.g. "40-30"
    serving: int                # 1 or 2
    true_probability: float     # probability player1 wins (0.0–1.0)
    last_update: float
    is_match_point: bool = False
    is_tiebreak: bool = False


@dataclass
class Position:
    ticker: str
    side: str                   # 'yes' or 'no'
    quantity: int
    entry_price: float          # cents integer (e.g. 45)
    entry_time: float           # time.time()
    stop_price: float           # cents
    target_price: float         # cents
    order_id: str
    rule_id: str                # which rule triggered the entry
    agent_name: str             # which agent set the rule
    contract_class: str         # SCALP / SWING / POSITION / WATCH
    edge_at_entry: float = 0.0
    opened_by_syndicate: bool = True  # False for externally-created positions
    # Trading philosophy exit parameters (rinse-and-repeat, never hold to settlement)
    target_exit_pct:  float = 0.20   # exit at +20% gain
    stop_loss_pct:    float = 0.30   # exit at -30% loss
    max_hold_minutes: int   = 60     # time stop at 60 min
    hold_to_settlement: bool = False # never hold to settlement


class SharedState:
    """Thread-safe shared memory for The Syndicate. All reads/writes must acquire the lock."""

    def __init__(self):
        self._lock = threading.Lock()

        # ticker → MarketData
        self.markets: dict[str, MarketData] = {}

        # match_id → TennisGame
        self.tennis_games: dict[str, TennisGame] = {}

        # ticker → Position
        self.open_positions: dict[str, Position] = {}

        # tickers with orders currently in-flight (dedup guard)
        self.pending_orders: set[str] = set()

        self.daily_pnl: float = 0.0
        self.daily_loss: float = 0.0  # track losses separately (positive number)
        self.is_trading: bool = True
        self.session_start: float = 0.0

        # Re-entry lockout: ticker → timestamp of last autonomous exit
        # Agents check this in _base_should_evaluate to block re-entry for 30 min
        self.exit_lockouts: dict[str, float] = {}

    # -------------------------------------------------------------------------
    # Markets
    # -------------------------------------------------------------------------

    def upsert_market(self, ticker: str, yes_price: float, no_bid: float,
                      volume_dollars: float, spread: float, days_to_settlement: float,
                      contract_class: str, series_ticker: str, ts: float):
        with self._lock:
            if ticker not in self.markets:
                self.markets[ticker] = MarketData(
                    ticker=ticker,
                    yes_price=yes_price,
                    no_bid=no_bid,
                    volume_dollars=volume_dollars,
                    spread=spread,
                    days_to_settlement=days_to_settlement,
                    contract_class=contract_class,
                    series_ticker=series_ticker,
                    last_update=ts,
                )
            m = self.markets[ticker]
            # Append to price history
            m.price_history.append((ts, yes_price))
            # Prune history older than 5 minutes
            cutoff = ts - 300
            m.price_history = [(t, p) for t, p in m.price_history if t >= cutoff]
            m.yes_price = yes_price
            m.no_bid = no_bid
            m.volume_dollars = volume_dollars
            m.spread = spread
            m.days_to_settlement = days_to_settlement
            m.contract_class = contract_class
            m.series_ticker = series_ticker
            m.last_update = ts

    def set_velocity(self, ticker: str, velocity: float, window_seconds: float):
        """Calculate and store velocity: percent price change over last window_seconds."""
        with self._lock:
            if ticker not in self.markets:
                return
            m = self.markets[ticker]
            ts = m.last_update
            cutoff = ts - window_seconds
            history_in_window = [(t, p) for t, p in m.price_history if t >= cutoff]
            if len(history_in_window) >= 2:
                oldest_price = history_in_window[0][1]
                newest_price = history_in_window[-1][1]
                if oldest_price > 0:
                    m.velocity = ((newest_price - oldest_price) / oldest_price) * 100
                else:
                    m.velocity = 0.0
            else:
                m.velocity = velocity  # fallback to caller-provided value

    def update_market_price(self, ticker: str, yes_price: float, no_bid: float,
                            volume_dollars: float, ts: float):
        """
        Update price fields only — spread/days_to_settlement/contract_class/series_ticker
        are NOT touched. Safe to call from the WS tick thread without overwriting
        metadata set by the scan engine heartbeat.
        If ticker is not yet seeded by the scan engine, the update is silently dropped.
        """
        with self._lock:
            if ticker not in self.markets:
                return
            m = self.markets[ticker]
            m.price_history.append((ts, yes_price))
            cutoff = ts - 300
            m.price_history = [(t, p) for t, p in m.price_history if t >= cutoff]
            m.yes_price = yes_price
            m.no_bid = no_bid
            m.volume_dollars = volume_dollars
            m.last_update = ts

    def get_market(self, ticker: str) -> Optional[MarketData]:
        with self._lock:
            return self.markets.get(ticker)

    def get_all_markets(self) -> dict:
        with self._lock:
            return dict(self.markets)

    def remove_market(self, ticker: str):
        with self._lock:
            self.markets.pop(ticker, None)

    # -------------------------------------------------------------------------
    # Tennis games
    # -------------------------------------------------------------------------

    def upsert_tennis_game(self, game: TennisGame):
        with self._lock:
            self.tennis_games[game.match_id] = game

    def get_tennis_game(self, match_id: str) -> Optional[TennisGame]:
        with self._lock:
            return self.tennis_games.get(match_id)

    def get_all_tennis_games(self) -> dict:
        with self._lock:
            return dict(self.tennis_games)

    def remove_tennis_game(self, match_id: str):
        with self._lock:
            self.tennis_games.pop(match_id, None)

    # -------------------------------------------------------------------------
    # Positions
    # -------------------------------------------------------------------------

    def add_position(self, position: Position):
        with self._lock:
            self.open_positions[position.ticker] = position

    def get_position(self, ticker: str) -> Optional[Position]:
        with self._lock:
            return self.open_positions.get(ticker)

    def get_all_positions(self) -> dict:
        with self._lock:
            return dict(self.open_positions)

    def remove_position(self, ticker: str) -> Optional[Position]:
        with self._lock:
            return self.open_positions.pop(ticker, None)

    def has_position(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self.open_positions

    def position_count(self) -> int:
        with self._lock:
            return len(self.open_positions)

    def get_positions_by_class(self, contract_class: str) -> dict:
        """Return all open positions matching a contract class."""
        with self._lock:
            return {
                ticker: pos
                for ticker, pos in self.open_positions.items()
                if pos.contract_class == contract_class
            }

    # -------------------------------------------------------------------------
    # Pending orders (dedup guard)
    # -------------------------------------------------------------------------

    def add_pending(self, ticker: str):
        with self._lock:
            self.pending_orders.add(ticker)

    def remove_pending(self, ticker: str):
        with self._lock:
            self.pending_orders.discard(ticker)

    def is_pending(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self.pending_orders

    # -------------------------------------------------------------------------
    # P&L
    # -------------------------------------------------------------------------

    def record_trade_pnl(self, pnl: float):
        """Update daily P&L. Call after every closed trade."""
        with self._lock:
            self.daily_pnl += pnl
            if pnl < 0:
                self.daily_loss += abs(pnl)

    def get_daily_loss(self) -> float:
        with self._lock:
            return self.daily_loss

    def get_daily_pnl(self) -> float:
        with self._lock:
            return self.daily_pnl

    def reset_daily(self):
        with self._lock:
            self.daily_pnl = 0.0
            self.daily_loss = 0.0

    # -------------------------------------------------------------------------
    # Exposure
    # -------------------------------------------------------------------------

    def get_total_exposure(self) -> float:
        """Sum of (entry_price / 100 * quantity) across all open positions."""
        with self._lock:
            return sum(
                (pos.entry_price / 100) * pos.quantity
                for pos in self.open_positions.values()
            )

    def get_exposure_by_class(self) -> dict:
        """Return {contract_class: total_exposure_dollars} for all open positions."""
        with self._lock:
            result: dict[str, float] = {}
            for pos in self.open_positions.values():
                exposure = (pos.entry_price / 100) * pos.quantity
                result[pos.contract_class] = result.get(pos.contract_class, 0.0) + exposure
            return result

    # -------------------------------------------------------------------------
    # Kill switch
    # -------------------------------------------------------------------------

    def halt_trading(self, reason: str):
        with self._lock:
            self.is_trading = False
        print(f"[STATE] TRADING HALTED — {reason}")

    def resume_trading(self):
        with self._lock:
            self.is_trading = True

    def trading_active(self) -> bool:
        with self._lock:
            return self.is_trading


# Singleton — import and use this everywhere
state = SharedState()
