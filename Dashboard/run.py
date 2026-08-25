"""War Room entrypoint. Boots scheduler in background + Flask web server."""
import os
import sys
import atexit

from apscheduler.schedulers.background import BackgroundScheduler

from app.main import app
from app.config import SCAN_INTERVAL_SEC, TRADING_MODE
from engine import scheduler as engine_sched


def main():
    # Initialize DB and run boot log
    engine_sched.boot()

    # Run an immediate scan so the dashboard isn't empty on first load
    print(f"[warroom] mode={TRADING_MODE} — running initial scan...")
    engine_sched.run_scan()

    # Start background scan loop
    bg = BackgroundScheduler(daemon=True)
    bg.add_job(engine_sched.run_scan, "interval",
               seconds=SCAN_INTERVAL_SEC, id="scan_loop",
               max_instances=1, coalesce=True)
    bg.start()
    atexit.register(lambda: bg.shutdown(wait=False))
    print(f"[warroom] scheduler running every {SCAN_INTERVAL_SEC}s")

    # Start Flask
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"[warroom] dashboard at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    sys.exit(main())
