"""
tennis_ws.py — ESPN unofficial REST poller for live ATP tennis scores.

Polls ESPN ATP scoreboard every N seconds (default 10s, configurable via
syndicate_config.yaml → tennis.poll_interval_seconds).

Same public interface as WebSocket version:
  class TennisWS:
    .start() -> Thread
    .stop()
    .is_alive() -> bool

Callback injection (by main.py):
  ws._on_game_live_callback = _on_game_live   # called on new live match
"""

import os
import sys
import re
import time
import threading
import logging
from typing import Optional

import requests

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from core.shared_state import state, TennisGame
from playbook.tennis_probability import (
    match_win_probability,
    parse_set_scores,
    is_match_point,
)

logger = logging.getLogger("syndicate.tennis_ws")

ESPN_URL        = "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard"
DEFAULT_P_SERVE = 0.64
_LIVE_STATUSES  = {
    "status_in_progress", "in_progress", "inprogress",
    "live", "1h", "2h", "active",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    import yaml
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _poll_interval() -> float:
    try:
        cfg = _load_config()
        return float(cfg.get("tennis", {}).get("poll_interval_seconds", 10))
    except Exception:
        return 10.0


# ---------------------------------------------------------------------------
# Fuzzy name matching
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z\s]", "", name)
    return " ".join(name.split())


def _name_similarity(a: str, b: str) -> float:
    a_tokens = set(_normalize_name(a).split())
    b_tokens = set(_normalize_name(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    if a_tokens == b_tokens:
        return 1.0
    a_surname = _normalize_name(a).split()[-1]
    b_surname = _normalize_name(b).split()[-1]
    if a_surname == b_surname:
        return 0.9
    overlap = len(a_tokens & b_tokens)
    return overlap / max(len(a_tokens), len(b_tokens))


# ---------------------------------------------------------------------------
# ESPN response parser
# ---------------------------------------------------------------------------

def _parse_espn_event(comp: dict) -> Optional[TennisGame]:
    """
    Parse one ESPN competition dict into a TennisGame, or None if not live.
    """
    match_id = str(comp.get("id", ""))
    if not match_id:
        return None

    # Status check — use state field ("in" = live)
    status_block = comp.get("status", {})
    state_str    = str(status_block.get("type", {}).get("state", "")).lower()

    if state_str != "in":
        if state_str == "post":
            state.remove_tennis_game(match_id)
        return None

    # Competitors
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    home_comp = next((c for c in competitors if c.get("homeAway") == "home"),
                     competitors[0])
    away_comp = next((c for c in competitors if c.get("homeAway") == "away"),
                     competitors[1])

    player1 = (home_comp.get("athlete", {}).get("displayName") or
                home_comp.get("athlete", {}).get("fullName") or
                home_comp.get("displayName", "Player 1"))
    player2 = (away_comp.get("athlete", {}).get("displayName") or
                away_comp.get("athlete", {}).get("fullName") or
                away_comp.get("displayName", "Player 2"))

    if not player1 or not player2:
        return None

    # Situation (live score detail)
    situation = comp.get("situation") or {}

    # Set scores from linescores (authoritative)
    home_lines = home_comp.get("linescores", [])
    away_lines = away_comp.get("linescores", [])
    set_list   = []
    if home_lines and away_lines:
        for h, a in zip(home_lines, away_lines):
            set_list.append((int(h.get("value", 0)), int(a.get("value", 0))))

    set_score_str = ", ".join(f"{g1}-{g2}" for g1, g2 in set_list)

    # Current set game count
    cur_set_block = situation.get("currentSet") or {}
    if cur_set_block:
        p1_games = int(cur_set_block.get("home", 0) or 0)
        p2_games = int(cur_set_block.get("away", 0) or 0)
    elif set_list:
        p1_games, p2_games = set_list[-1]
    else:
        p1_games, p2_games = 0, 0

    # Game score
    gs_block   = situation.get("gameScore") or {}
    home_pts   = str(gs_block.get("home", "0") or "0")
    away_pts   = str(gs_block.get("away", "0") or "0")
    game_score = f"{home_pts}-{away_pts}"

    # Sets won
    p1_sets_won = 0
    p2_sets_won = 0
    completed_sets = set_list[:-1] if set_list else []
    for g1, g2 in completed_sets:
        if g1 > g2:
            p1_sets_won += 1
        else:
            p2_sets_won += 1

    # Server
    serving_raw = str(situation.get("serving") or situation.get("possession") or "home").lower()
    server = 1 if "home" in serving_raw else 2

    # Best-of
    periods = comp.get("format", {}).get("regulation", {}).get("periods", 3)
    best_of = int(periods or 3)

    # Win probability
    try:
        true_prob = match_win_probability(
            p1_sets=p1_sets_won,
            p2_sets=p2_sets_won,
            p1_games=p1_games,
            p2_games=p2_games,
            game_score=game_score,
            server=server,
            best_of=best_of,
            p_serve=DEFAULT_P_SERVE,
        )
    except Exception as e:
        logger.debug("[TennisPoller] Prob calc error %s: %s", match_id, e)
        true_prob = 0.5

    # Match point
    try:
        match_point = is_match_point(
            p1_sets_won, p2_sets_won, p1_games, p2_games,
            game_score, server, best_of,
        )
    except Exception:
        match_point = False

    return TennisGame(
        match_id=match_id,
        player1=player1,
        player2=player2,
        score_raw=set_score_str,
        set_scores=set_list,
        current_set=len(set_list),
        current_game=game_score,
        serving=server,
        true_probability=true_prob,
        last_update=time.time(),
        is_match_point=match_point,
        is_tiebreak=False,
    )


# ---------------------------------------------------------------------------
# TennisWS — REST poller with same public interface as WebSocket version
# ---------------------------------------------------------------------------

class TennisWS:
    """
    Polls ESPN ATP scoreboard every N seconds.
    Same .start() / .stop() / .is_alive() interface as WebSocket version.

    main.py injects the game-live callback after construction:
        ws._on_game_live_callback = _on_game_live
    """

    def __init__(self):
        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._session  = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; Syndicate/1.0)",
            "Accept":     "application/json",
        })

        # Injected by main.py after construction
        self._on_game_live_callback = None

        # Track match IDs seen as live — to detect new-game events
        self._live_match_ids: set = set()
        self._seen_lock = threading.Lock()

    def _poll_once(self):
        """Fetch scoreboard and upsert all live matches into shared_state."""
        try:
            resp = self._session.get(ESPN_URL, timeout=10)
            if resp.status_code != 200:
                logger.warning("[TennisPoller] ESPN returned %d", resp.status_code)
                return
            data = resp.json()

            tournaments  = data.get("events", [])
            competitions = []
            for tourney in tournaments:
                for grouping in tourney.get("groupings", []):
                    competitions.extend(grouping.get("competitions", []))

            updated  = 0
            new_live = []

            for comp in competitions:
                game = _parse_espn_event(comp)
                if game:
                    state.upsert_tennis_game(game)
                    updated += 1

                    # Detect new game-live transitions
                    with self._seen_lock:
                        if game.match_id not in self._live_match_ids:
                            self._live_match_ids.add(game.match_id)
                            new_live.append(game)

            # Fire game-live callback for newly detected live matches
            if new_live and self._on_game_live_callback is not None:
                for game in new_live:
                    try:
                        self._on_game_live_callback(
                            game.match_id, game.player1, game.player2
                        )
                    except Exception as e:
                        logger.error("[TennisPoller] game_live callback error: %s", e)

            # Clean up match IDs that are no longer live
            current_live = {
                str(c.get("id", ""))
                for t in tournaments
                for g in t.get("groupings", [])
                for c in g.get("competitions", [])
                if str(c.get("status", {}).get("type", {}).get("state", "")).lower() == "in"
            }
            with self._seen_lock:
                self._live_match_ids &= current_live

            logger.debug("[TennisPoller] %d tournaments, %d matches, %d live, %d new.",
                         len(tournaments), len(competitions), updated, len(new_live))

        except requests.exceptions.Timeout:
            logger.warning("[TennisPoller] ESPN request timed out.")
        except Exception as e:
            logger.error("[TennisPoller] Poll error: %s", e)

    def _run_loop(self):
        logger.info("[TennisPoller] Started. Polling ESPN every %gs.", _poll_interval())
        while self._running:
            self._poll_once()
            interval = _poll_interval()
            # Sleep in 1s chunks so stop() responds quickly
            for _ in range(int(interval)):
                if not self._running:
                    break
                time.sleep(1)
        logger.info("[TennisPoller] Stopped.")

    def start(self) -> threading.Thread:
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop,
            name="tennis-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info("[TennisPoller] Daemon thread started.")
        return self._thread

    def stop(self):
        self._running = False

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Kalshi ticker player-code parser
# ---------------------------------------------------------------------------

def _parse_ticker_players(ticker: str) -> list:
    """
    Extract 3-char player abbreviation codes from Kalshi tennis ticker.
    Kalshi format: SERIES-{YY}{MON}{DD}{P1CODE}{P2CODE}-{WINNER_CODE}
    e.g. 'KXATPMATCH-26APR10AUGSIN-SIN' → ['aug', 'sin']
    """
    parts = ticker.upper().split("-")
    if len(parts) < 3:
        return []

    middle     = parts[1]
    name_block = re.sub(r"^\d+[A-Z]+\d+", "", middle)

    codes: list = []
    if len(name_block) >= 6:
        codes.append(name_block[:3].lower())
        codes.append(name_block[3:6].lower())
    elif len(name_block) >= 4:
        codes.append(name_block[:3].lower())
        rest = name_block[3:].lower()
        if rest not in codes:
            codes.append(rest)
    elif len(name_block) >= 2:
        codes.append(name_block[:3].lower() if len(name_block) >= 3 else name_block.lower())

    # Include the explicit winner/side code (last segment)
    winner = parts[-1]
    if 2 <= len(winner) <= 4 and winner.isalpha():
        w = winner[:3].lower() if len(winner) >= 3 else winner.lower()
        if w not in codes:
            codes.append(w)

    return codes


def _player_code_match(player_name: str, codes: list) -> float:
    tokens = _normalize_name(player_name).split()
    for code in codes:
        for token in tokens:
            if len(code) >= 3 and token.startswith(code):
                return 1.0
            if len(code) == 2 and token == code:
                return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# match_game_to_ticker — public interface used by brain/scan engine
# ---------------------------------------------------------------------------

def match_game_to_ticker(ticker: str) -> Optional[TennisGame]:
    """
    Given a Kalshi ticker string, find the matching live TennisGame.

    Primary: parse 3-char player codes from Kalshi ticker format.
    Fallback: fuzzy token matching.

    Returns TennisGame or None.
    """
    all_games = state.get_all_tennis_games()
    if not all_games:
        return None

    # Primary: code-based matching
    player_codes = _parse_ticker_players(ticker)
    if player_codes:
        best_game  = None
        best_score = 0.0
        for game in all_games.values():
            p1 = _player_code_match(game.player1, player_codes)
            p2 = _player_code_match(game.player2, player_codes)
            combined = (p1 + p2) / 2
            if combined > best_score and combined >= 0.5:
                best_score = combined
                best_game  = game
        if best_game:
            return best_game

    # Fallback: fuzzy token matching
    ticker_text   = ticker.lower().replace("-", " ").replace("_", " ")
    ticker_tokens = set(ticker_text.split())
    best_game  = None
    best_score = 0.0
    for game in all_games.values():
        p1_score = max(
            (_name_similarity(game.player1, t) for t in ticker_tokens),
            default=0.0,
        )
        p2_score = max(
            (_name_similarity(game.player2, t) for t in ticker_tokens),
            default=0.0,
        )
        combined = (p1_score + p2_score) / 2
        if combined > best_score and combined >= 0.5:
            best_score = combined
            best_game  = game

    return best_game
