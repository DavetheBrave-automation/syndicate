"""warroom/run.py — Start the NFE War Room dashboard."""
import os
import sys

# Ensure syndicate root is on path
_WARROOM_DIR    = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT = os.path.dirname(_WARROOM_DIR)
sys.path.insert(0, _SYNDICATE_ROOT)

from app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"[WarRoom] Starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
