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


def _leaderboard(trades: list[dict]) -> list[dict]:
    stats: dict[str, dict] = {}
    for t in trades:
        name = t.get("agent_name") or "UNKNOWN"
        if name not in stats:
            stats[name] = {"name": name, "trades": 0, "wins": 0, "pnl": 0.0}
        stats[name]["trades"] += 1
        pnl = float(t.get("pnl") or 0)
        stats[name]["pnl"] = round(stats[name]["pnl"] + pnl, 4)
        if pnl > 0:
            stats[name]["wins"] += 1

    board = []
    for s in stats.values():
        t = s["trades"]
        s["win_pct"] = round(s["wins"] / t * 100, 0) if t else 0
        board.append(s)

    return sorted(board, key=lambda x: -x["pnl"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    cfg     = _load_config()
    signals = _load_signals()
    mems    = _load_agent_memories()
    trades  = _load_trades()
    log_lines = _tail_log(40)
    leaderboard = _leaderboard(trades)

    # Validation phase
    vp   = cfg.get("validation_phase", {})
    risk = cfg.get("risk", {})

    # Agent roster cards
    roster = []
    for name in _KNOWN_AGENTS:
        mem  = mems.get(name, {})
        perf = mem.get("performance", {})
        t    = int(perf.get("trades", 0))
        w    = int(perf.get("wins", 0))
        pnl  = float(perf.get("total_pnl", 0.0))
        wrate = round(w / t * 100) if t else 0
        roster.append({
            "name":     name,
            "trades":   t,
            "win_pct":  wrate,
            "pnl":      round(pnl, 2),
            "benched":  mem.get("benched", False),
            "domain":   mem.get("domain", ""),
            "is_new":   name == "OIL",
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
