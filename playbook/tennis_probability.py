"""
tennis_probability.py — Markov chain win probability for live tennis matches.

Given current match state (score, set, server), calculates the probability
that player 1 wins the match. Uses Markov chain forward computation.

Model assumptions:
  - Server wins each point with probability p_serve (default 0.64, tour average)
  - Receiver wins each point with probability 1 - p_serve
  - Tiebreak: both players win points at 0.50 (equal)
  - Best-of-3 sets (standard for most ATP/WTA)

Score string formats we parse:
  Game: "0", "15", "30", "40", "A" (advantage)
  Set:  "6-4" or "7-6"
  Match: player1_sets vs player2_sets

Usage:
  prob = match_win_probability(
      p1_sets=1, p2_sets=0,
      p1_games=3, p2_games=2,
      game_score="40-30",
      server=1,              # 1 = player1 serving, 2 = player2 serving
      best_of=3,
      p_serve=0.64,
  )
"""

from functools import lru_cache


# ---------------------------------------------------------------------------
# Point probability: probability server wins a game
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _prob_server_wins_game(p: float) -> float:
    """
    Probability server wins a game given point-win probability p.
    Uses closed-form via deuce probability.
    """
    q = 1.0 - p
    p_win_from_deuce = (p * p) / (p * p + q * q)

    @lru_cache(maxsize=None)
    def pw(s, r):
        if s >= 4 and s >= r + 2:
            return 1.0
        if r >= 4 and r >= s + 2:
            return 0.0
        if s == 3 and r == 3:
            return p_win_from_deuce
        return p * pw(s + 1, r) + q * pw(s, r + 1)

    return pw(0, 0)


@lru_cache(maxsize=None)
def _prob_server_wins_game_from_score(p: float, s: int, r: int) -> float:
    """
    Probability server wins game from current score (s, r).
    s, r: 0=love, 1=15, 2=30, 3=40 (4=advantage state after deuce)
    """
    q = 1.0 - p
    p_win_from_deuce = (p * p) / (p * p + q * q)

    @lru_cache(maxsize=None)
    def pw(sv, rv):
        if sv >= 4 and sv >= rv + 2:
            return 1.0
        if rv >= 4 and rv >= sv + 2:
            return 0.0
        if sv == 3 and rv == 3:
            return p_win_from_deuce
        return p * pw(sv + 1, rv) + q * pw(sv, rv + 1)

    return pw(s, r)


@lru_cache(maxsize=None)
def _prob_server_wins_tiebreak_from(p: float, s: int, r: int) -> float:
    """
    Probability server wins tiebreak from current score (s, r).
    Tiebreak: first to 7 wins, must lead by 2. Both serve roughly equal.
    """
    q = 1.0 - p
    p_win_deuce = (p * p) / (p * p + q * q)

    @lru_cache(maxsize=None)
    def pw(sv, rv):
        if sv >= 7 and sv >= rv + 2:
            return 1.0
        if rv >= 7 and rv >= sv + 2:
            return 0.0
        if sv == 6 and rv == 6:
            return p_win_deuce
        return p * pw(sv + 1, rv) + q * pw(sv, rv + 1)

    return pw(s, r)


# ---------------------------------------------------------------------------
# Set probability
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _prob_server_wins_set_from(p_serve: float, sg: int, rg: int,
                                is_tiebreak: bool, tb_s: int, tb_r: int) -> float:
    """
    Probability that the current server wins the set.

    p_serve: probability server wins a service point
    sg, rg: current game count (server, receiver) within this set
    is_tiebreak: whether currently in a tiebreak
    tb_s, tb_r: tiebreak point score if is_tiebreak
    """
    q_serve = 1.0 - p_serve
    p_serve_game = _prob_server_wins_game(p_serve)
    p_recv_game  = 1.0 - _prob_server_wins_game(q_serve)

    if is_tiebreak:
        tb_p = 0.5
        return _prob_server_wins_tiebreak_from(tb_p, tb_s, tb_r)

    @lru_cache(maxsize=None)
    def ps(sv, rv, server_serves_next):
        if sv >= 6 and sv >= rv + 2:
            return 1.0
        if rv >= 6 and rv >= sv + 2:
            return 0.0
        if sv == 7:
            return 1.0
        if rv == 7:
            return 0.0
        if sv == 6 and rv == 6:
            return _prob_server_wins_tiebreak_from(0.5, 0, 0)

        if server_serves_next:
            p_win_this_game = p_serve_game
        else:
            p_win_this_game = p_recv_game

        next_serves = not server_serves_next

        return (p_win_this_game * ps(sv + 1, rv, next_serves) +
                (1 - p_win_this_game) * ps(sv, rv + 1, next_serves))

    return ps(sg, rg, server_serves_next=True)


# ---------------------------------------------------------------------------
# Match probability
# ---------------------------------------------------------------------------

def match_win_probability(
    p1_sets: int,
    p2_sets: int,
    p1_games: int,
    p2_games: int,
    game_score: str,
    server: int,
    best_of: int = 3,
    p_serve: float = 0.64,
) -> float:
    """
    Probability player 1 wins the match from current state.

    Args:
        p1_sets:    Sets won by player 1
        p2_sets:    Sets won by player 2
        p1_games:   Games won in current set by player 1
        p2_games:   Games won in current set by player 2
        game_score: Current game score string, e.g. "40-30", "0-15", "A-40"
        server:     1 = player 1 serving, 2 = player 2 serving
        best_of:    3 or 5
        p_serve:    Probability the server wins any given point (default 0.64)

    Returns:
        float: probability player 1 wins match (0.0–1.0)
    """
    sets_to_win = (best_of + 1) // 2

    if p1_sets >= sets_to_win:
        return 1.0
    if p2_sets >= sets_to_win:
        return 0.0

    gs, gr, is_tiebreak, tb_s, tb_r = _parse_game_score(game_score)

    if server == 1:
        p_server_wins_point = p_serve
    else:
        p_server_wins_point = 1.0 - p_serve

    if server == 1:
        if is_tiebreak:
            p1_wins_game = _prob_server_wins_tiebreak_from(0.5, tb_s, tb_r)
        else:
            p1_wins_game = _prob_server_wins_game_from_score(p_serve, gs, gr)
    else:
        if is_tiebreak:
            p1_wins_game = 1.0 - _prob_server_wins_tiebreak_from(0.5, tb_s, tb_r)
        else:
            p2_wins_game = _prob_server_wins_game_from_score(p_serve, gs, gr)
            p1_wins_game = 1.0 - p2_wins_game

    p_p1_wins_set_from_current = _calc_p1_wins_current_set(
        p1_games, p2_games, p1_wins_game, server, p_serve
    )

    result = _calc_match_win_prob(
        p1_sets, p2_sets,
        p_p1_wins_set_from_current,
        server, p_serve, sets_to_win,
        first_set=True,
    )
    return result


def _calc_p1_wins_current_set(p1_games: int, p2_games: int, p1_wins_cur_game: float,
                               server: int, p_serve: float) -> float:
    """Probability P1 wins the current set, given p1_wins_cur_game."""
    q_p1_wins_game = 1.0 - p1_wins_cur_game

    p_p1_serve_game = _prob_server_wins_game(p_serve)
    p_p1_recv_game  = 1.0 - _prob_server_wins_game(1.0 - p_serve)

    @lru_cache(maxsize=None)
    def ps(g1, g2, p1_serves_next):
        if g1 >= 6 and g1 >= g2 + 2:
            return 1.0
        if g2 >= 6 and g2 >= g1 + 2:
            return 0.0
        if g1 == 7:
            return 1.0
        if g2 == 7:
            return 0.0
        if g1 == 6 and g2 == 6:
            return 0.5

        p_win = p_p1_serve_game if p1_serves_next else p_p1_recv_game
        nxt   = not p1_serves_next
        return p_win * ps(g1 + 1, g2, nxt) + (1 - p_win) * ps(g1, g2 + 1, nxt)

    p1_serves_next_game = (server == 1)

    p_after_p1_wins  = ps(p1_games + 1, p2_games, not p1_serves_next_game)
    p_after_p1_loses = ps(p1_games, p2_games + 1, not p1_serves_next_game)

    return (p1_wins_cur_game * p_after_p1_wins +
            q_p1_wins_game * p_after_p1_loses)


def _calc_match_win_prob(p1_sets: int, p2_sets: int, p1_wins_cur_set: float,
                          server: int, p_serve: float, sets_to_win: int,
                          first_set: bool) -> float:
    """Forward match probability from current set scores."""
    p_p1_wins_future_set = _calc_future_set_prob(p_serve)

    @lru_cache(maxsize=None)
    def pm(s1, s2, cur_set_done):
        if s1 >= sets_to_win:
            return 1.0
        if s2 >= sets_to_win:
            return 0.0
        if not cur_set_done:
            return (p1_wins_cur_set * pm(s1 + 1, s2, True) +
                    (1 - p1_wins_cur_set) * pm(s1, s2 + 1, True))
        else:
            return (p_p1_wins_future_set * pm(s1 + 1, s2, True) +
                    (1 - p_p1_wins_future_set) * pm(s1, s2 + 1, True))

    return pm(p1_sets, p2_sets, False)


def _calc_future_set_prob(p_serve: float) -> float:
    """Long-run probability P1 wins a set (both players serve equally)."""
    return _prob_server_wins_set_from(p_serve, 0, 0, False, 0, 0)


# ---------------------------------------------------------------------------
# Score parser
# ---------------------------------------------------------------------------

_SCORE_MAP = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4}


def _parse_game_score(score_str: str):
    """
    Parse game score string into (server_pts, receiver_pts, is_tiebreak, tb_s, tb_r).
    Handles: "40-30", "0-0", "A-40", "40-A", "6-5" (tiebreak points)
    """
    if not score_str or score_str in ("-", ""):
        return 0, 0, False, 0, 0

    parts = str(score_str).strip().split("-")
    if len(parts) != 2:
        return 0, 0, False, 0, 0

    left, right = parts[0].strip(), parts[1].strip()

    try:
        l_int, r_int = int(left), int(right)
        if l_int <= 20 and r_int <= 20:
            return l_int, r_int, True, l_int, r_int
    except ValueError:
        pass

    l_pts = _SCORE_MAP.get(left.upper(), 0)
    r_pts = _SCORE_MAP.get(right.upper(), 0)
    return l_pts, r_pts, False, 0, 0


def parse_set_scores(set_str: str) -> list:
    """
    Parse set score string like "6-4, 3-2" into list of tuples.
    Returns [(6,4), (3,2)] — (p1_games, p2_games) per set.
    """
    result = []
    sets   = set_str.replace(",", " ").split()
    for s in sets:
        if "-" in s:
            parts = s.split("-")
            try:
                result.append((int(parts[0]), int(parts[1])))
            except (ValueError, IndexError):
                pass
    return result


# ---------------------------------------------------------------------------
# Match point detection
# ---------------------------------------------------------------------------

def is_match_point(p1_sets: int, p2_sets: int, p1_games: int, p2_games: int,
                    game_score: str, server: int, best_of: int = 3) -> bool:
    """
    Returns True if either player is on match point.
    Match point: one point away from winning the match.
    """
    sets_to_win = (best_of + 1) // 2

    def _one_point_away(p1s, p2s, p1g, p2g, gs):
        if p1s == sets_to_win - 1:
            if p1g == 5 and p2g <= 4:
                return True
            if p1g == 6 and p2g == 6:
                return False
            if p1g >= 5 and p2g >= 5:
                return False
        return False

    p1_match_point = _one_point_away(p1_sets, p2_sets, p1_games, p2_games, game_score)
    p2_match_point = _one_point_away(p2_sets, p1_sets, p2_games, p1_games, game_score)
    return p1_match_point or p2_match_point
