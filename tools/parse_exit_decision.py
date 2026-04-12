"""
parse_exit_decision.py — Parse TC exit decision and write structured JSON.

Called by wake_syndicate.ps1 after TC responds to an exit review.
Usage: python tools/parse_exit_decision.py --agent ACE

Reads:  triggers/{agent}_exit_decision.txt  (raw TC text output from wake_syndicate.ps1)
Writes: triggers/{agent}_exit_decision.json (structured, read by main.py gate poll loop)

Output JSON:
  {
    "agent":     "ACE",
    "ticker":    "KXATPMATCH-...",
    "decision":  "EXIT" | "HOLD",
    "urgency":   "immediate" | "within_5min" | "no_rush",
    "reasoning": "..."
  }
"""

import os
import sys
import json
import argparse
import logging

logger = logging.getLogger("syndicate.parse_exit")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRIGGERS_DIR   = os.path.join(_SYNDICATE_ROOT, "triggers")


def _extract_json(text: str) -> dict | None:
    """
    Extract the first complete JSON object from text using brace-depth counting.
    Returns None if no valid object found.
    """
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = text[start : i + 1]
                try:
                    return json.loads(fragment)
                except json.JSONDecodeError:
                    # Reset and keep looking
                    depth = 0
                    start = -1
    return None


def parse_exit_decision(agent_name: str) -> int:
    """
    Parse the exit decision text file for agent_name.
    Returns 0 on success, 1 on failure.
    """
    agent_name = agent_name.upper()

    txt_path  = os.path.join(_TRIGGERS_DIR, f"{agent_name.lower()}_exit_decision.txt")
    json_path = os.path.join(_TRIGGERS_DIR, f"{agent_name.lower()}_exit_decision.json")
    tmp_path  = json_path + ".tmp"

    if not os.path.exists(txt_path):
        logger.error("Exit decision text not found: %s", txt_path)
        return 1

    try:
        with open(txt_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except Exception as e:
        logger.error("Could not read %s: %s", txt_path, e)
        return 1

    parsed = _extract_json(raw)
    if parsed is None:
        logger.error("No JSON found in exit decision text for %s", agent_name)
        # Write a safe default — HOLD so we don't force-exit without data
        parsed = {
            "decision":  "HOLD",
            "urgency":   "no_rush",
            "reasoning": "parse_exit_decision: no JSON found in TC response — defaulting to HOLD",
        }

    # Normalise decision field
    decision = str(parsed.get("decision", "HOLD")).upper()
    if decision not in ("EXIT", "HOLD"):
        logger.warning("Unexpected decision value '%s' — normalising to HOLD", decision)
        decision = "HOLD"

    output = {
        "agent":     agent_name,
        "decision":  decision,
        "urgency":   parsed.get("urgency", "no_rush"),
        "reasoning": parsed.get("reasoning", ""),
        "ticker":    parsed.get("ticker", ""),
    }

    try:
        os.makedirs(_TRIGGERS_DIR, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        os.replace(tmp_path, json_path)
        logger.info(
            "Exit decision written: %s → %s | urgency=%s",
            agent_name, decision, output["urgency"],
        )
    except Exception as e:
        logger.error("Could not write exit decision JSON: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return 1

    # Clean up source text file
    try:
        os.remove(txt_path)
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Parse TC exit decision")
    ap.add_argument("--agent", required=True, help="Agent name (e.g. ACE)")
    args = ap.parse_args()
    sys.exit(parse_exit_decision(args.agent))
