"""
intelligence/parse_decision.py — Parse TC panel output and write decision.json.

Called by wake_syndicate.ps1 after TC responds.
Reads:  intelligence/tc_analysis.txt
        triggers/pending_signal.json  (path passed as sys.argv[1])
Writes: triggers/decision.json
Posts:  Discord panel summary (if webhook configured)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

ANALYSIS_PATH = os.path.join(_SYNDICATE_ROOT, "intelligence", "tc_analysis.txt")
DECISION_PATH = os.path.join(_SYNDICATE_ROOT, "triggers", "decision.json")


# ---------------------------------------------------------------------------
# Regex extractors
# ---------------------------------------------------------------------------

def _extract_verdict(text: str, agent: str, options: list[str]) -> str | None:
    """
    Extract VERDICT from an === AGENT === section.
    Returns the first matching option found after the agent header, or None.
    """
    pattern = re.compile(
        rf"===\s*{agent}\s*===.*?VERDICT:\s*({'|'.join(options)})",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        return m.group(1).strip().upper()
    return None


def _extract_field(text: str, agent: str, field: str) -> str | None:
    """Extract a named field from an agent section (e.g. EDGE_PCT, RECOMMENDED_SIZE)."""
    pattern = re.compile(
        rf"===\s*{agent}\s*===.*?{field}:\s*([^\n]+)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Voting rules (authoritative — Python code decides, TC output is cross-check)
# ---------------------------------------------------------------------------

def apply_voting_rules(
    scout: str,
    quant: str,
    banker: str,
    wire: str,
    timekeeper: str,
    oracle: str,
) -> str:
    """
    Apply the panel voting rules in priority order.
    Returns: EXECUTE | REDUCE | DELAY | BLOCK
    """
    unfav_scout      = scout == "UNFAVORABLE"
    unfav_quant      = quant in ("NO_EDGE", "NEGATIVE_EDGE")
    unfav_banker     = banker == "BLOCK"
    unfav_wire       = wire == "RED_FLAG"
    unfav_timekeeper = timekeeper == "TOO_LATE"
    unfav_oracle     = oracle == "WARNS_AGAINST"

    unfav_count = sum([
        unfav_scout, unfav_quant, unfav_banker,
        unfav_wire, unfav_timekeeper, unfav_oracle,
    ])

    # ── HARD BLOCKS (evaluated in priority order) ──

    # 1. Wire=RED_FLAG AND any other unfavorable
    if unfav_wire and unfav_count > 1:
        return "BLOCK"

    # 2. Quant=NEGATIVE_EDGE (negative expected value — never trade)
    if quant == "NEGATIVE_EDGE":
        return "BLOCK"

    # 3. Banker=BLOCK (risk management override — no exceptions)
    if unfav_banker:
        return "BLOCK"

    # 4. Timekeeper=TOO_LATE (entry window closed)
    if unfav_timekeeper:
        return "BLOCK"

    # 5. Scout=UNFAVORABLE AND Quant=NO_EDGE
    if unfav_scout and quant == "NO_EDGE":
        return "BLOCK"

    # 6. Quant=NO_EDGE (no mathematical basis)
    if quant == "NO_EDGE":
        return "BLOCK"

    # 7. Any two agents unfavorable
    if unfav_count >= 2:
        return "BLOCK"

    # ── DELAY ──
    # Only timekeeper says WAIT, everything else OK
    if timekeeper == "WAIT" and unfav_count <= 1:
        return "DELAY"

    # ── EXECUTE / REDUCE ──
    if quant == "EDGE" and banker in ("APPROVE", "REDUCE"):
        favorable_count = sum([
            scout == "FAVORABLE",
            wire == "CLEAR",
            timekeeper == "GOOD",
            oracle in ("CONFIRMED", "MIXED"),
        ])
        if favorable_count >= 2:
            if banker == "REDUCE":
                return "REDUCE"
            if unfav_oracle:
                return "REDUCE"
            return "EXECUTE"

    # Default: not enough confidence
    return "BLOCK"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    signal_path = sys.argv[1] if len(sys.argv) > 1 else None

    # Read TC analysis
    if not os.path.exists(ANALYSIS_PATH):
        print(f"[Parser] tc_analysis.txt not found at {ANALYSIS_PATH}")
        sys.exit(1)

    with open(ANALYSIS_PATH, encoding="utf-8") as f:
        tc_text = f.read()

    # Read signal JSON for context
    ticker         = "UNKNOWN"
    contract_class = "SCALP"
    conviction_tier = ""
    signal_data    = {}
    _order_fields: dict = {}

    if signal_path and os.path.exists(signal_path):
        try:
            with open(signal_path, encoding="utf-8") as f:
                signal_data = json.load(f)
            sig = signal_data.get("signal", {})
            ticker          = sig.get("ticker", "UNKNOWN")
            contract_class  = sig.get("contract_class", "SCALP")
            conviction_tier = sig.get("conviction_tier", "")
            _order_fields   = {k: sig.get(k) for k in (
                "side", "entry_price", "target_price", "stop_price",
                "max_size_dollars", "reasoning", "agent_name",
            )}
        except Exception as e:
            print(f"[Parser] Could not read signal JSON: {e}")

    # ── Extract verdicts ──
    scout      = _extract_verdict(tc_text, "SCOUT",      ["FAVORABLE", "NEUTRAL", "UNFAVORABLE"]) or "NEUTRAL"
    quant      = _extract_verdict(tc_text, "QUANT",      ["EDGE", "NO_EDGE", "NEGATIVE_EDGE"])     or "NO_EDGE"
    banker     = _extract_verdict(tc_text, "BANKER",     ["APPROVE", "REDUCE", "BLOCK"])           or "BLOCK"
    wire       = _extract_verdict(tc_text, "WIRE",       ["CLEAR", "CAUTION", "RED_FLAG"])         or "CAUTION"
    timekeeper = _extract_verdict(tc_text, "TIMEKEEPER", ["GOOD", "WAIT", "TOO_LATE"])             or "GOOD"
    oracle     = _extract_verdict(tc_text, "ORACLE",     ["CONFIRMED", "MIXED", "WARNS_AGAINST"])  or "MIXED"

    # ── Extract QUANT numeric fields ──
    edge_pct_raw = _extract_field(tc_text, "QUANT", "EDGE_PCT")
    rec_size_raw = _extract_field(tc_text, "QUANT", "RECOMMENDED_SIZE")

    edge_pct = 0.0
    try:
        edge_pct = float(edge_pct_raw) if edge_pct_raw else 0.0
    except ValueError:
        pass

    rec_size = 0
    try:
        rec_size = int(float(rec_size_raw)) if rec_size_raw else 0
    except ValueError:
        pass

    # Cap size at $50 (hard ceiling regardless of QUANT recommendation)
    rec_size = min(rec_size, 50)

    # ── Apply voting rules (Python is authoritative) ──
    decision = apply_voting_rules(
        scout=scout,
        quant=quant,
        banker=banker,
        wire=wire,
        timekeeper=timekeeper,
        oracle=oracle,
    )

    # For REDUCE: halve the recommended size (floor at $1)
    final_size = rec_size
    if decision == "REDUCE":
        final_size = max(1, rec_size // 2)

    print(
        f"[Parser] Verdicts: scout={scout} quant={quant} banker={banker} "
        f"wire={wire} timekeeper={timekeeper} oracle={oracle}"
    )
    print(f"[Parser] Decision: {decision} | Size: ${final_size} | Edge: {edge_pct:.1f}%")

    # ── Write decision.json ──
    decision_doc = {
        "ticker":          ticker,
        "contract_class":  contract_class,
        "conviction_tier": conviction_tier,
        "decision":        decision,
        "size":            final_size,
        "edge_pct":        edge_pct,
        # Order fields passed through from the signal so main.py can place the order
        "side":            _order_fields.get("side", "yes"),
        "entry_price":     _order_fields.get("entry_price"),
        "target_price":    _order_fields.get("target_price"),
        "stop_price":      _order_fields.get("stop_price"),
        "agent_name":      _order_fields.get("agent_name", "TC_GATE"),
        "reasoning":       _order_fields.get("reasoning", ""),
        "verdicts": {
            "scout":      scout,
            "quant":      quant,
            "banker":     banker,
            "wire":       wire,
            "timekeeper": timekeeper,
            "oracle":     oracle,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs(os.path.dirname(DECISION_PATH), exist_ok=True)
    with open(DECISION_PATH, "w", encoding="utf-8") as f:
        json.dump(decision_doc, f, indent=2)

    print(f"[Parser] decision.json written: {DECISION_PATH}")

    # ── Discord panel summary ──
    _post_discord_summary(decision_doc, signal_data)


def _post_discord_summary(decision: dict, signal_data: dict):
    """Post the panel decision to Discord (best-effort; never raises)."""
    try:
        import yaml
        cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        webhook = cfg.get("notifications", {}).get("discord_webhook", "")
        if not webhook:
            return
    except Exception:
        return

    try:
        import urllib.request

        ticker = decision["ticker"]
        dec    = decision["decision"]
        size   = decision["size"]
        edge   = decision["edge_pct"]
        v      = decision["verdicts"]
        cls    = decision.get("contract_class", "")
        tier   = decision.get("conviction_tier", "")

        colors = {
            "EXECUTE": 3066993,   # green
            "REDUCE":  16426522,  # yellow
            "DELAY":   7506394,   # blue
            "BLOCK":   15158332,  # red
        }
        icons = {
            "EXECUTE": "EXECUTE",
            "REDUCE":  "REDUCE",
            "DELAY":   "DELAY",
            "BLOCK":   "BLOCK",
        }

        def _row(label, verdict, good_vals, mid_vals):
            if verdict in good_vals:
                dot = "[G]"
            elif verdict in mid_vals:
                dot = "[Y]"
            else:
                dot = "[R]"
            return f"{dot} **{label}:** `{verdict}`"

        lines = [
            f"**{tier}** | `{ticker}` | `{cls}`\n",
            _row("Scout",      v["scout"],      ["FAVORABLE"],           ["NEUTRAL"]),
            _row("Quant",      v["quant"],      ["EDGE"],                []),
            _row("Banker",     v["banker"],     ["APPROVE"],             ["REDUCE"]),
            _row("Wire",       v["wire"],       ["CLEAR"],               ["CAUTION"]),
            _row("Timekeeper", v["timekeeper"], ["GOOD"],                ["WAIT"]),
            _row("Oracle",     v["oracle"],     ["CONFIRMED", "MIXED"],  []),
            "",
            f"**Edge:** `{edge:.1f}%` | **Size:** `${size}`",
        ]

        payload = json.dumps({
            "embeds": [{
                "title": f"[Syndicate] TC Panel: {icons.get(dec, dec)}",
                "description": "\n".join(lines),
                "color": colors.get(dec, 7506394),
            }]
        }).encode("utf-8")

        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)

    except Exception as e:
        print(f"[Parser] Discord post failed: {e}")


if __name__ == "__main__":
    main()
