"""
get_echo_warning.py — Print ECHO pattern warning for a given signal.

Called by wake_syndicate.ps1 before each TC agent signal decision.
Output is captured by PS1 and injected into the TC prompt.

Usage: python tools/get_echo_warning.py --ticker KXATPMATCH-26APR10-SIN --agent ACE [--yes_price 0.45]

Must complete in < 2 seconds.
"""

import os
import sys
import argparse
import logging

# Suppress logging output so only the warning string goes to stdout
logging.disable(logging.CRITICAL)

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)


def main():
    ap = argparse.ArgumentParser(description="Get ECHO panel warning")
    ap.add_argument("--ticker",    required=True)
    ap.add_argument("--agent",     required=True)
    ap.add_argument("--yes_price", type=float, default=0.5)
    args = ap.parse_args()

    try:
        from agents.echo import EchoAgent
        echo    = EchoAgent()
        warning = echo.get_panel_warning_from_ticker(
            args.ticker, args.agent.upper(), args.yes_price
        )
    except Exception as e:
        warning = f"ECHO: Unavailable — {e}"

    print(warning)


if __name__ == "__main__":
    main()
