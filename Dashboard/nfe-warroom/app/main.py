"""Flask web layer. Single dashboard route."""
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, jsonify

from app import db
from app.config import (
    TRADING_MODE, SCORE_THRESHOLD, OIL_BOOST,
    EXPOSURE_CAP_PCT, SPORTS_CAP_PCT,
)
from engine import kalshi, executor, scheduler
from signals import aggregate

ROOT = Path(__file__).resolve().parent.parent
app = Flask(__name__, template_folder=str(ROOT / "templates"))


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_hms(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")


@app.route("/")
def dashboard():
    last_scan_ts = db.state_get("last_scan_ts", 0)
    last_scan = _fmt_ts(last_scan_ts) if last_scan_ts else "Never"

    signals = db.state_get("signals_snapshot") or aggregate.snapshot()
    macro_rows = aggregate.macro_table_rows(signals)

    log_rows = db.recent_log(limit=20)
    for r in log_rows:
        r["ts_str"] = _fmt_hms(r["ts"])

    positions = db.all_positions()
    opportunities = db.state_get("opportunities") or []

    return render_template(
        "dashboard.html",
        mode=TRADING_MODE,
        last_scan=last_scan,
        scan_count=db.state_get("scan_count", 0),
        session_pnl=executor.session_pnl(),
        cash=executor.get_cash(),
        portfolio_value=executor.portfolio_value(),
        exposure=executor.total_exposure(),
        market_count=db.state_get("market_count", 0),
        score_threshold=SCORE_THRESHOLD,
        oil_boost=OIL_BOOST,
        exposure_cap_pct=int(EXPOSURE_CAP_PCT * 100),
        sports_cap_pct=int(SPORTS_CAP_PCT * 100),
        kalshi_status="LIVE" if kalshi.has_creds() else "MOCK",
        positions=positions,
        opportunities=opportunities,
        orders=[],  # TODO: wire kalshi.get_orders() in LIVE
        macro_rows=macro_rows,
        trade_log=log_rows,
    )


@app.route("/healthz")
def health():
    return jsonify({
        "ok": True,
        "mode": TRADING_MODE,
        "kalshi_creds": kalshi.has_creds(),
        "last_scan_ts": db.state_get("last_scan_ts", 0),
        "scan_count": db.state_get("scan_count", 0),
    })


@app.route("/scan", methods=["POST", "GET"])
def manual_scan():
    """Trigger a scan on demand. Useful for testing."""
    scheduler.run_scan()
    return jsonify({"ok": True, "scan_count": db.state_get("scan_count", 0)})
