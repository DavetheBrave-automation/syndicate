"""
get_sage_briefing.py — Print SAGE historical briefing for a given market ticker.

Called by wake_syndicate.ps1 before each TC agent signal decision.
Output is captured by PS1 and injected into the TC prompt.

Usage: python tools/get_sage_briefing.py --ticker KXATPMATCH-26APR10-SIN [--yes_price 0.45] [--class SCALP]

Must complete in < 2 seconds.
"""

import os
import sys
import argparse
import json
import logging

# Suppress logging output so only the briefing string goes to stdout
logging.disable(logging.CRITICAL)

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)


class _FakeMarket:
    """Minimal market object for SAGE.get_panel_briefing()."""
    def __init__(self, ticker: str, yes_price: float, contract_class: str, series_ticker: str):
        self.ticker          = ticker
        self.yes_price       = yes_price
        self.contract_class  = contract_class
        self.series_ticker   = series_ticker
        self.volume_dollars  = 10_000  # dummy — not used in briefing
        self.velocity        = 0.0


def main():
    ap = argparse.ArgumentParser(description="Get SAGE panel briefing")
    ap.add_argument("--ticker",    required=True)
    ap.add_argument("--yes_price", type=float, default=0.5)
    ap.add_argument("--class_",    dest="contract_class", default="SCALP")
    args = ap.parse_args()

    series = args.ticker.split("-")[0].upper()

    try:
        from agents.sage import SageAgent
        sage   = SageAgent()
        market = _FakeMarket(
            ticker         = args.ticker,
            yes_price      = args.yes_price,
            contract_class = args.contract_class,
            series_ticker  = series,
        )
        briefing = sage.get_panel_briefing_str(market)
    except Exception as e:
        briefing = f"SAGE: Unavailable — {e}"

    print(briefing)


if __name__ == "__main__":
    main()
