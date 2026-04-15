"""
warroom/app.py — NFE War Room × Syndicate Engine dashboard.
Read-only Flask app — reads directly from Syndicate data files.
No imports from Syndicate's runtime modules (no shared_state, no scan_engine).
"""
import os
import sys
import json
import sqlite3
import glob
from datetime import datetime, timezone
from collections import defaultdict

import yaml
from flask import Flask, render_template, jsonify

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_WARROOM_DIR    = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT = os.path.dirname(_WARROOM_DIR)

sys.path.insert(0, _SYNDICATE_ROOT)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.jinja_env.globals["enumerate"] = enumerate

_DB_PATH    = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_LOG_PATH   = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate.log")
_MEMORY_DIR = os.path.join(_SYNDICATE_ROOT, "memory")
_CACHE_DIR  = os.path.join(_SYNDICATE_ROOT, "signals", "cache")
_CFG_PATH   = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")

_KNOWN_AGENTS = [
    "ACE", "AXIOM", "DIAMOND", "PHOENIX", "BLITZ",
    "GHOST", "ENDGAME", "SHADOW", "ORACLE", "TIDE",
    "CIPHER", "DELTA", "MIRROR", "SAGE", "ECHO", "OIL",
]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_signals() -> dict:
    s: dict = {}
    for fname in ("fred.json", "fng.json", "macro_llm.json"):
        path = os.path.join(_CACHE_DIR, fname)
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                s.update({k: v for k, v in data.items() if not k.startswith("_")})
        except Exception:
            pass
    return s


def _load_agent_memories() -> dict[str, dict]:
    memories: dict[str, dict] = {}
    for name in _KNOWN_AGENTS:
        path = os.path.join(_MEMORY_DIR, f"{name}.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    memories[name] = json.load(f)
        except Exception:
            memories[name] = {}
    return memories


def _load_trades() -> list[dict]:
    if not os.path.exists(_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT ticker, side, entry_price, exit_price, quantity,
                   pnl, exit_reason, agent_name, contract_class,
                   entry_time, exit_time, hold_seconds
            FROM syndicate_trades
            ORDER BY id DESC
            LIMIT 200
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _tail_log(n: int = 40) -> list[str]:
    if not os.path.exists(_LOG_PATH):
        return []
    try:
        with open(_LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def _load_agent_stats() -> dict[str, dict]:
    """Canonical agent stats from DB — used for both roster tiles and leaderboard."""
    if not os.path.exists(_DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT agent_name,
                   COUNT(*) as trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(COALESCE(SUM(pnl), 0), 2) as total_pnl
            FROM syndicate_trades
            WHERE exit_time IS NOT NULL
            GROUP BY agent_name
        """)
        result = {}
        for r in cur.fetchall():
            name, trades, wins, pnl = r[0], int(r[1]), int(r[2] or 0), float(r[3] or 0.0)
            result[name] = {
                "name":    name,
                "trades":  trades,
                "wins":    wins,
                "win_pct": round(wins / trades * 100) if trades else 0,
                "pnl":     pnl,
            }
        conn.close()
        return result
    except Exception:
        return {}


def _get_agent_trade_history(agent_name: str) -> list:
    """Per-agent closed trade history, newest first, max 50."""
    if not os.path.exists(_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT ticker, side, quantity, entry_price, exit_price,
                   pnl, exit_reason, entry_time, exit_time
            FROM syndicate_trades
            WHERE agent_name = ? AND exit_time IS NOT NULL
            ORDER BY exit_time DESC
            LIMIT 50
        """, (agent_name,))
        trades = []
        for r in cur.fetchall():
            ticker, side, qty, entry_cents, exit_raw, pnl, reason, opened, closed = r
            pnl         = float(pnl or 0)
            entry_cents = int(entry_cents or 0)
            exit_cents  = int(round(float(exit_raw or 0) * 100)) if exit_raw else 0
            ticker_short = ticker.split("-")[-1] if ticker else "?"
            direction    = "↑ YES" if side == "yes" else "↓ NO"
            pnl_str      = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            trades.append({
                "ticker":       ticker,
                "ticker_short": ticker_short,
                "side":         side,
                "qty":          qty,
                "entry":        entry_cents,
                "exit":         exit_cents,
                "pnl":          pnl,
                "pnl_str":      pnl_str,
                "reason":       (reason or "")[:32],
                "opened":       opened,
                "closed":       closed,
                "direction":    direction,
            })
        conn.close()
        return trades
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    cfg         = _load_config()
    signals     = _load_signals()
    mems        = _load_agent_memories()
    trades      = _load_trades()
    log_lines   = _tail_log(40)
    agent_stats = _load_agent_stats()
    leaderboard = sorted(agent_stats.values(), key=lambda x: -x["pnl"])

    # Validation phase
    vp   = cfg.get("validation_phase", {})
    risk = cfg.get("risk", {})

    # Agent roster cards — stats from DB (same source as leaderboard)
    roster = []
    for name in _KNOWN_AGENTS:
        mem   = mems.get(name, {})
        stats = agent_stats.get(name, {"trades": 0, "wins": 0, "win_pct": 0, "pnl": 0.0})
        roster.append({
            "name":     name,
            "trades":   stats["trades"],
            "win_pct":  stats["win_pct"],
            "pnl":      round(stats["pnl"], 2),
            "benched":  mem.get("benched", False),
            "domain":   mem.get("domain", ""),
            "is_new":   name == "OIL",
            "history":  _get_agent_trade_history(name),
        })

    # Recent trades (last 15)
    recent_trades = trades[:15]
    for t in recent_trades:
        t["pnl_sign"] = "+" if float(t.get("pnl") or 0) >= 0 else ""

    # Macro signal rows
    macro_rows = [
        {"label": "Fed Rate",      "val": signals.get("fed_status", "—"),     "raw": signals.get("fed_funds_rate")},
        {"label": "DXY",           "val": signals.get("dxy_status", "—"),     "raw": signals.get("dxy")},
        {"label": "Yield Curve",   "val": signals.get("curve_status", "—"),   "raw": signals.get("yield_curve")},
        {"label": "Fear & Greed",  "val": f"{signals.get('fng_value', '—')} {signals.get('fng_status', '')}",  "raw": None},
        {"label": "Oil Regime",    "val": f"{signals.get('oil_regime_score', '—')} {signals.get('oil_narrative', '')[:60] if signals.get('oil_narrative') else ''}",  "raw": None},
        {"label": "Market Risk",   "val": signals.get("overall_market_risk", "—"),  "raw": None},
        {"label": "Top Class",     "val": signals.get("top_opportunity_class", "—").upper(),  "raw": None},
        {"label": "Claude Macro",  "val": signals.get("macro_llm_status", "NO_KEY"),  "raw": None},
    ]

    # Total trades and P&L
    total_pnl    = round(sum(float(t.get("pnl") or 0) for t in trades), 2)
    total_trades = len(trades)

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return render_template(
        "dashboard.html",
        mode          = "PAPER" if cfg.get("syndicate", {}).get("paper_mode", True) else "LIVE",
        now_utc       = now_utc,
        roster        = roster,
        macro_rows    = macro_rows,
        vp            = vp,
        leaderboard   = leaderboard,
        recent_trades = recent_trades,
        log_lines     = log_lines,
        total_pnl     = total_pnl,
        total_trades  = total_trades,
        signals       = signals,
        max_exposure  = risk.get("max_total_exposure", 50),
    )


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "mode": "PAPER", "agents": len(_KNOWN_AGENTS)})


@app.route("/api/signals")
def api_signals():
    return jsonify(_load_signals())


@app.route("/api/leaderboard")
def api_leaderboard():
    trades = _load_trades()
    return jsonify(_leaderboard(trades))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
