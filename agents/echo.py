"""
echo.py — ECHO Reinforcement Learning Agent. Always on the decision panel.

ECHO never trades. It grades every trade decision after the fact and surfaces
warnings when TC is about to repeat a previously failed pattern.

Grades: A (confirmed edge, win) / B (confirmed edge, bad luck small loss) /
        C (unclear edge, win = lucky) / D (unclear edge, loss) / F (warning ignored)

After 3 D/F grades on the same pattern, ECHO flags it for TC review.
ECHO's memory is the system's long-term intelligence — never prune it.

ECHO's weekly report (Sundays, 6hr scan) synthesizes wins, fails, and changes.
"""

import os
import sys
import json
import logging
import sqlite3
from datetime import datetime, timezone

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.echo")

_DB_PATH      = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_ECHO_MEMORY  = os.path.join(_SYNDICATE_ROOT, "memory", "ECHO.json")
_DF_THRESHOLD = 3    # flag pattern after 3 D/F grades

# Grade thresholds
_SMALL_LOSS     = -0.50   # loss < $0.50 may be "bad luck B" not "D"
_CLEAR_EDGE_MIN = 0.10    # edge_pct / 100 — above this = "confirmed edge"


def _extract_series(ticker: str) -> str:
    return ticker.split("-")[0].upper() if ticker else ""


def _price_bucket(yes_price: float) -> str:
    p = int(yes_price * 100)
    if p < 20: return "0-20"
    elif p < 40: return "20-40"
    elif p < 60: return "40-60"
    elif p < 80: return "60-80"
    else: return "80-100"


def _make_pattern_key(agent_name: str, series: str, price_bkt: str) -> str:
    return f"{agent_name}|{series}|{price_bkt}"


class EchoAgent(BaseAgent):
    name   = "ECHO"
    domain = "all"

    ALWAYS_ON_PANEL = True   # injected into every TC panel prompt

    seed_rules = [
        "Never trade independently — ECHO only grades and improves other agents",
        "After every closed trade, score the entry decision: A/B/C/D/F",
        "A = edge confirmed, win. B = edge confirmed, small loss (bad luck). C = edge unclear, win (lucky). D = edge unclear, loss. F = clear warning ignored",
        "Track which agents produce A/B decisions vs D/F decisions",
        "Track which TC panel arguments were right vs wrong over time",
        "Flag any agent with F-grade rate > 30% for strategy review",
        "Surface patterns: what reasoning leads to wins? What reasoning leads to losses?",
        "Write weekly insight report to memory: top 3 patterns that worked, top 3 that failed",
        "If same mistake made 3 times — write a new collective rule flag for TC review",
        "ECHO memory is the system long-term intelligence — never prune it",
    ]

    def __init__(self, config=None):
        super().__init__()

    # =========================================================================
    # ECHO never trades
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        return False   # ECHO never initiates trades

    def evaluate(self, market, game=None) -> None:
        return None    # ECHO never submits buy signals

    # =========================================================================
    # Panel warning — called before every TC decision
    # =========================================================================

    def get_panel_warning(self, signal: dict) -> str:
        """
        Returns a one-line warning if ECHO recognises a previously failed pattern.
        Returns "CLEAR: No adverse history for this pattern." if no issues.
        """
        ticker     = signal.get("signal", {}).get("ticker", "")
        agent_name = signal.get("signal", {}).get("agent_name", "")
        yes_price  = signal.get("signal", {}).get("entry_price", 0.5)
        series     = _extract_series(ticker)
        bkt        = _price_bucket(float(yes_price))
        pattern    = _make_pattern_key(agent_name, series, bkt)

        mem    = self._load_echo_memory()
        grades = mem.get("pattern_grades", {}).get(pattern, [])

        if not grades:
            return "ECHO: CLEAR — No adverse history for this pattern."

        df_count = sum(1 for g in grades if g in ("D", "F"))
        total    = len(grades)
        f_count  = sum(1 for g in grades if g == "F")

        if f_count >= 2:
            return (
                f"ECHO: ⚠️  WARNING — {agent_name} {series} {bkt}: "
                f"F-grade {f_count}x — TC warnings previously ignored on this pattern. "
                f"High scrutiny required."
            )
        if df_count >= _DF_THRESHOLD:
            return (
                f"ECHO: ⚠️  WARNING — {agent_name} {series} {bkt}: "
                f"{df_count}/{total} trades graded D/F. This pattern has a history of poor decisions. "
                f"Increase skepticism."
            )
        if df_count >= 2:
            return (
                f"ECHO: CAUTION — {agent_name} {series} {bkt}: "
                f"{df_count} D/F grades on this pattern. Monitor closely."
            )

        return f"ECHO: CLEAR — {agent_name} {series} {bkt}: {total} trades, {total - df_count} A/B/C grades."

    def get_panel_warning_from_ticker(self, ticker: str, agent_name: str, yes_price: float) -> str:
        """Convenience method for tool scripts."""
        signal = {
            "signal": {
                "ticker":     ticker,
                "agent_name": agent_name,
                "entry_price": yes_price,
            }
        }
        return self.get_panel_warning(signal)

    # =========================================================================
    # Trade grading — called by outcome_reporter after every close
    # =========================================================================

    def grade_trade(self, trade_record: dict) -> dict:
        """
        Grade a closed trade.
        trade_record expected keys: agent_name, ticker, side, entry_price (cents),
                                    pnl, exit_reason, edge_pct (optional).
        Returns: {grade, agent, reasoning, lesson, pattern_flag}
        """
        agent_name  = trade_record.get("agent_name", "unknown")
        ticker      = trade_record.get("ticker", "")
        pnl         = float(trade_record.get("pnl", 0.0) or 0.0)
        exit_reason = trade_record.get("exit_reason", "")
        entry_cents = float(trade_record.get("entry_price", 50) or 50)
        edge_pct    = float(trade_record.get("edge_pct", 0.0) or 0.0)
        series      = _extract_series(ticker)
        bkt         = _price_bucket(entry_cents / 100.0)
        pattern     = _make_pattern_key(agent_name, series, bkt)

        won     = pnl > 0
        has_edge = edge_pct >= (_CLEAR_EDGE_MIN * 100)

        # Grade logic
        if has_edge and won:
            grade   = "A"
            reason  = f"Edge confirmed ({edge_pct:.1f}%), trade won. Textbook execution."
            lesson  = None
        elif has_edge and not won and pnl > _SMALL_LOSS:
            grade   = "B"
            reason  = f"Edge confirmed ({edge_pct:.1f}%), small loss ${pnl:.2f} — likely bad luck, not bad decision."
            lesson  = None
        elif not has_edge and won:
            grade   = "C"
            reason  = f"Low edge ({edge_pct:.1f}%), won ${pnl:.2f} — lucky, but process was weak."
            lesson  = f"Review {agent_name} {series} signals — winning without clear edge is not sustainable."
        elif not has_edge and not won:
            grade   = "D"
            reason  = f"Low edge ({edge_pct:.1f}%), lost ${pnl:.2f} — poor process and poor outcome."
            lesson  = f"{agent_name} {series} {bkt}: edge was insufficient. Consider raising minimum threshold."
        else:
            # F: large loss with high supposed edge (something fundamentally wrong)
            grade   = "F" if pnl < -1.0 else "D"
            reason  = f"{'Large loss' if pnl < -1.0 else 'Loss'} ${pnl:.2f} despite edge_pct={edge_pct:.1f}% — review {agent_name} edge calculation for {series}."
            lesson  = f"ALERT: {agent_name} edge model may be broken on {series}. Investigate immediately."

        # Update echo memory
        mem = self._load_echo_memory()
        pg  = mem.setdefault("pattern_grades", {})
        pg.setdefault(pattern, []).append(grade)

        # Track per-agent overall grades
        ag = mem.setdefault("agent_grades", {})
        ag.setdefault(agent_name, []).append(grade)

        # Store grade records (last 200)
        gr = mem.setdefault("grade_records", [])
        gr.append({
            "grade":       grade,
            "agent":       agent_name,
            "ticker":      ticker,
            "pnl":         round(pnl, 4),
            "pattern":     pattern,
            "reason":      reason,
            "lesson":      lesson,
            "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if len(gr) > 200:
            mem["grade_records"] = gr[-200:]

        # Pattern flag: 3+ D/F on same pattern
        df_count = sum(1 for g in pg.get(pattern, []) if g in ("D", "F"))
        pattern_flag = df_count >= _DF_THRESHOLD

        if pattern_flag:
            flags = mem.setdefault("pattern_flags", [])
            if pattern not in flags:
                flags.append(pattern)
                logger.warning(
                    "[ECHO] Pattern flag: %s has %d D/F grades — flagging for TC review",
                    pattern, df_count,
                )

        self._save_echo_memory(mem)

        result = {
            "grade":        grade,
            "agent":        agent_name,
            "reasoning":    reason,
            "lesson":       lesson,
            "pattern_flag": pattern_flag,
        }
        logger.info(
            "[ECHO] Graded %s %s: %s | pnl=$%.2f | %s",
            agent_name, ticker, grade, pnl, reason[:80],
        )
        return result

    # =========================================================================
    # Weekly report
    # =========================================================================

    def write_weekly_report(self) -> None:
        """
        Synthesize last 7 days of grades.
        Writes memory/echo_weekly_report.json and posts summary to Telegram.
        """
        mem     = self._load_echo_memory()
        records = mem.get("grade_records", [])

        # Filter last 7 days
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = [r for r in records if r.get("timestamp", "") >= cutoff]

        if not recent:
            logger.info("[ECHO] Weekly report: no trades in last 7 days.")
            return

        # Top 3 patterns that worked (A/B grades)
        ab_patterns: dict[str, int] = {}
        df_patterns: dict[str, int] = {}
        for r in recent:
            p = r.get("pattern", "unknown")
            if r["grade"] in ("A", "B"):
                ab_patterns[p] = ab_patterns.get(p, 0) + 1
            elif r["grade"] in ("D", "F"):
                df_patterns[p] = df_patterns.get(p, 0) + 1

        top_winners = sorted(ab_patterns.items(), key=lambda x: -x[1])[:3]
        top_losers  = sorted(df_patterns.items(), key=lambda x: -x[1])[:3]

        total      = len(recent)
        ab_count   = sum(1 for r in recent if r["grade"] in ("A", "B"))
        df_count   = sum(1 for r in recent if r["grade"] in ("D", "F"))
        quality    = round(ab_count / total * 100, 1) if total else 0.0

        report = {
            "period":          "last_7_days",
            "total_grades":    total,
            "ab_grades":       ab_count,
            "df_grades":       df_count,
            "decision_quality": quality,
            "top_patterns":    top_winners,
            "weak_patterns":   top_losers,
            "pattern_flags":   mem.get("pattern_flags", []),
            "generated_at":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        # Write report
        report_path = os.path.join(_SYNDICATE_ROOT, "memory", "echo_weekly_report.json")
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            logger.info("[ECHO] Weekly report written: quality=%.1f%% (%d grades)", quality, total)
        except Exception as e:
            logger.error("[ECHO] Weekly report write failed: %s", e)

        # Telegram notification
        try:
            import notifications.telegram as tg
            summary = (
                f"ECHO Weekly Report | Quality: {quality:.1f}% ({ab_count}A/B, {df_count}D/F)\n"
                f"Top patterns: {', '.join(p for p, _ in top_winners) or 'none'}\n"
                f"Weak patterns: {', '.join(p for p, _ in top_losers) or 'none'}\n"
                f"Flags: {len(mem.get('pattern_flags', []))}"
            )
            tg.post(summary, "📊")
        except Exception:
            pass

    # =========================================================================
    # Echo memory helpers (separate from agent memory in base_agent)
    # =========================================================================

    def _load_echo_memory(self) -> dict:
        """Load ECHO-specific extended memory from memory/ECHO.json."""
        try:
            if os.path.exists(_ECHO_MEMORY):
                with open(_ECHO_MEMORY, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {
            "pattern_grades":  {},
            "agent_grades":    {},
            "grade_records":   [],
            "pattern_flags":   [],
        }

    def _save_echo_memory(self, mem: dict) -> None:
        """Atomic write of ECHO extended memory."""
        tmp = _ECHO_MEMORY + ".tmp"
        try:
            os.makedirs(os.path.dirname(_ECHO_MEMORY), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mem, f, indent=2)
            os.replace(tmp, _ECHO_MEMORY)
        except Exception as e:
            logger.error("[ECHO] Memory save failed: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass
