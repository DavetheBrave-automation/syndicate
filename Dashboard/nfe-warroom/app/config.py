"""Central configuration. All env-driven knobs live here."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Mode
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER").upper()
assert TRADING_MODE in ("PAPER", "LIVE"), "TRADING_MODE must be PAPER or LIVE"

# Kalshi
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")

# External signal APIs
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")

# Engine knobs
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "60"))
OIL_BOOST = int(os.getenv("OIL_BOOST", "5"))
EXPOSURE_CAP_PCT = float(os.getenv("EXPOSURE_CAP_PCT", "0.65"))
SPORTS_CAP_PCT = float(os.getenv("SPORTS_CAP_PCT", "0.15"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))

# Paper portfolio
PAPER_STARTING_CASH = float(os.getenv("PAPER_STARTING_CASH", "1000.00"))

# Storage
DB_PATH = ROOT / "data" / "warroom.db"
DB_PATH.parent.mkdir(exist_ok=True)

# Hard exit rules (hours-to-expiry triggers)
EXIT_T_MINUS_HOURS = {
    "soft_profit_exit": 6,   # T-6h: profitable -> limit sell at bid-2¢
    "hard_loss_exit": 3,     # T-3h: at loss/flat -> market sell
    "emergency_exit": 1,     # T-1h: market sell everything regardless
}

# Asset class detection by ticker prefix
ASSET_CLASS_PREFIXES = {
    "KXWTI": "oil", "KXBRENT": "oil", "KXOIL": "oil",
    "KXNG": "natgas",
    "KXGOLD": "gold",
    "KXBTC": "btc", "KXETH": "eth", "KXSOL": "sol",
    "KXMLB": "sports", "KXNHL": "sports", "KXNBA": "sports",
    "KXNFL": "sports", "KXNCAA": "sports", "KXMLBSPREAD": "sports",
    "KXUSDJPY": "forex", "KXEURUSD": "forex", "KXDXY": "forex",
    "KXFFR": "rates", "KX10Y": "rates", "KXYIELD": "rates",
    "KXPOTUS": "politics", "KXSEN": "politics", "KXHOUSE": "politics",
    "KXSPX": "stocks", "KXNDX": "stocks",
}

# Permanently blocked prefixes (no valid signal model)
BLOCKED_PREFIXES = (
    "KXMVE",       # multi-game extended esports
    "KXCROSS",     # cross-category parlays
    "KXMULTI",     # multi-event parlays
)
