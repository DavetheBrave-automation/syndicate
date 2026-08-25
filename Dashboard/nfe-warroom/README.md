# NFE War Room — Kalshi Event Contract Trading Engine

A multi-factor scoring + Kelly-sized auto-trader for Kalshi binary event contracts.
Modeled after the Dudu Pipi War Room v3 reference dashboard, rebuilt as a clean,
modular, paper-trade-first system.

## Architecture

```
warroom/
├── app/              # Flask web layer
│   ├── __init__.py
│   ├── main.py       # Flask app, routes, dashboard render
│   └── config.py     # Env vars, thresholds, caps
├── signals/          # External data feeds (cached)
│   ├── __init__.py
│   ├── fred.py       # Fed Funds Rate, DXY, yield curve
│   ├── crypto.py     # BTC Fear & Greed (alternative.me)
│   ├── coinglass.py  # Crypto funding rates (stub)
│   ├── congress.py   # Congressional trades (stub)
│   └── macro_llm.py  # Claude-powered narrative scoring (stub)
├── engine/           # Core trading logic
│   ├── __init__.py
│   ├── kalshi.py     # Kalshi API client (auth, markets, orders, positions)
│   ├── scoring.py    # 100-point multi-factor score per asset class
│   ├── sizing.py     # Half-Kelly position sizing
│   ├── allowlist.py  # Ticker prefix whitelist/blacklist
│   ├── executor.py   # Entry, stop/target monitoring, T-minus expiry rules
│   └── scheduler.py  # 60s scan loop
├── templates/
│   └── dashboard.html  # War room UI (matches reference styling)
├── static/           # (currently empty, all CSS inlined)
├── data/
│   └── warroom.db    # SQLite: positions, trade log, session state
├── requirements.txt
├── .env.example
└── run.py            # Entrypoint
```

## Data sources

| Source            | Free? | Key required | Refresh |
|-------------------|-------|--------------|---------|
| Kalshi API        | Account + KYC | Yes (member id + key) | Real-time |
| FRED              | Yes   | Yes (free)            | 4 hours |
| alternative.me    | Yes   | No                    | 1 hour |
| Coinglass         | Free tier | Optional (paid for full) | 15 min |
| Quiver Quant      | Free tier | Optional (paid for full) | 2 hours |
| Anthropic (Claude)| No    | Yes                   | On scan |

## Modes

- `PAPER` (default) — no real orders placed, all fills simulated against current bid/ask
- `LIVE` — real Kalshi order placement (requires explicit env flag)

**Always start in PAPER. Run for at least 7 days before flipping to LIVE.**

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in keys
python run.py
# dashboard: http://localhost:5000
```

## Status

- ✅ Scaffolded
- ✅ Dashboard template (matches reference)
- ✅ FRED + alternative.me signals (live)
- ✅ Scoring engine (100-pt model)
- ✅ Half-Kelly sizing
- ✅ Allowlist
- ✅ Paper executor + SQLite trade log
- ✅ 60s scan loop
- ⚠️  Kalshi client (auth scaffolded, needs real key to test)
- 🔲 Coinglass live integration
- 🔲 Congress live integration (Quiver or scrape)
- 🔲 Claude macro_llm integration
- 🔲 Live execution mode (only after paper validation)
