"""
update_memory.py — Apply a TC postmortem lesson to an agent's memory file.

Called by wake_syndicate.ps1 immediately after writing {name}_lesson.json.

Usage:
    python intelligence/update_memory.py <path_to_lesson_json>

Lesson JSON format (written by TC via postmortem_prompt.txt):
    {
      "lesson": "Entry too early — price continued moving against.",
      "new_rule": "Wait for volume to exceed 30000 before entering.",
      "modify_rule_index": null,
      "modified_rule": null
    }

Memory JSON format (memory/{name}.json, owned by BaseAgent):
    {
      "name": "ACE",
      "rules": ["rule1", "rule2", ...],   # max MAX_RULES entries
      "lessons": ["lesson1", ...],         # last MAX_LESSONS entries
      "performance": {...},
      "loss_streak": 0,
      "benched": false,
      "benched_until": null
    }

Rule update logic:
  1. If modify_rule_index is an int and modified_rule is non-empty:
     replace rules[modify_rule_index] with modified_rule.
  2. Append new_rule (if non-empty) to rules.
  3. If len(rules) > MAX_RULES: prune oldest PRUNE_COUNT rules.
  4. Append lesson text to lessons (cap at MAX_LESSONS).
  5. Log warning if loss_streak >= 5 (benching handled by BaseAgent).

Exits 0 on success, 1 on error. Removes lesson file on success.
"""

import os
import sys
import json
import logging

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_INTEL_DIR      = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT = os.path.dirname(_INTEL_DIR)

if _SYNDICATE_ROOT not in sys.path:
    sys.path.insert(0, _SYNDICATE_ROOT)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_RULES   = 15   # hard ceiling on rules per agent
PRUNE_COUNT = 3    # number of oldest rules removed when ceiling is hit
MAX_LESSONS = 50   # keep last N lesson strings in memory

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("syndicate.update_memory")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: str, data: dict) -> None:
    """Write dict to path atomically via a .tmp file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_memory(memory_path: str, agent_name: str) -> dict:
    """Load memory JSON; return default structure on missing/corrupt file."""
    if not os.path.exists(memory_path):
        logger.info("[%s] No memory file found — initialising default.", agent_name)
        return {
            "name":       agent_name,
            "rules":      [],
            "lessons":    [],
            "performance": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
            "loss_streak":   0,
            "benched":       False,
            "benched_until": None,
        }
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("memory file is not a JSON object")
        return data
    except Exception as e:
        logger.warning("[%s] Memory file corrupt (%s) — using default.", agent_name, e)
        return {
            "name":       agent_name,
            "rules":      [],
            "lessons":    [],
            "performance": {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
            "loss_streak":   0,
            "benched":       False,
            "benched_until": None,
        }


# ---------------------------------------------------------------------------
# Core update function
# ---------------------------------------------------------------------------


def update_memory(lesson_path: str) -> None:
    """
    Read a lesson JSON file, apply its rules update to the agent's memory,
    then delete the lesson file.

    Raises on any unrecoverable error (caller exits 1).
    """
    # ── 1. Read and validate lesson file ────────────────────────────────────
    with open(lesson_path, "r", encoding="utf-8") as f:
        lesson = json.load(f)

    if not isinstance(lesson, dict):
        raise ValueError(f"lesson file is not a JSON object: {lesson_path}")

    lesson_text   = (lesson.get("lesson")         or "").strip()
    new_rule_raw  = (lesson.get("new_rule")        or "").strip()
    modify_idx    = lesson.get("modify_rule_index")   # int or null
    modified_rule = (lesson.get("modified_rule")   or "").strip()

    # ── 2. Derive agent name from filename ───────────────────────────────────
    # triggers/ace_lesson.json  →  "ace"  →  "ACE"
    filename   = os.path.basename(lesson_path)
    agent_name = filename.replace("_lesson.json", "").upper()
    if not agent_name:
        raise ValueError(f"Cannot derive agent name from lesson filename: {filename}")

    # ── 3. Load agent memory ─────────────────────────────────────────────────
    memory_path = os.path.join(_SYNDICATE_ROOT, "memory", f"{agent_name}.json")
    memory      = _load_memory(memory_path, agent_name)

    rules   = memory.setdefault("rules",   [])
    lessons = memory.setdefault("lessons", [])

    # ── 4. Apply rule modification ───────────────────────────────────────────
    if modify_idx is not None and modified_rule:
        # TC may emit an int or a float (e.g. 0.0) — accept either whole number
        _idx_valid = (
            isinstance(modify_idx, (int, float))
            and float(modify_idx).is_integer()
        )
        _idx = int(modify_idx) if _idx_valid else -1
        if _idx_valid and 0 <= _idx < len(rules):
            old_rule = rules[_idx]
            rules[_idx] = modified_rule
            logger.info(
                "[%s] Rule %d modified:\n  OLD: %s\n  NEW: %s",
                agent_name, _idx, old_rule, modified_rule,
            )
        else:
            logger.warning(
                "[%s] modify_rule_index=%s invalid or out of range (rules=%d) — skipping modification.",
                agent_name, modify_idx, len(rules),
            )

    # ── 5. Append new rule ───────────────────────────────────────────────────
    if new_rule_raw:
        rules.append(new_rule_raw)
        logger.info("[%s] New rule appended: '%s'", agent_name, new_rule_raw)
    else:
        logger.debug("[%s] new_rule is empty — nothing appended.", agent_name)

    # ── 6. Prune oldest rules if ceiling exceeded ────────────────────────────
    if len(rules) > MAX_RULES:
        removed  = rules[:PRUNE_COUNT]
        rules[:] = rules[PRUNE_COUNT:]
        logger.warning(
            "[%s] Rules ceiling hit (%d > %d) — pruned %d oldest: %s",
            agent_name, len(rules) + PRUNE_COUNT, MAX_RULES, len(removed), removed,
        )

    memory["rules"] = rules

    # ── 7. Append lesson to history ──────────────────────────────────────────
    if lesson_text:
        lessons.append(lesson_text)
        if len(lessons) > MAX_LESSONS:
            lessons[:] = lessons[-MAX_LESSONS:]
        memory["lessons"] = lessons
        logger.debug("[%s] Lesson appended (%d total).", agent_name, len(lessons))

    # ── 8. Flag high loss streak (benching handled by BaseAgent) ────────────
    loss_streak = memory.get("loss_streak", 0)
    if loss_streak >= 5:
        logger.warning(
            "[%s] ATTENTION: loss_streak=%d — agent is or should be benched.",
            agent_name, loss_streak,
        )

    # ── 9. Atomic write ──────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    _atomic_write(memory_path, memory)
    logger.info(
        "[%s] Memory saved — %d rules, %d lessons, loss_streak=%d",
        agent_name, len(rules), len(lessons), loss_streak,
    )

    # ── 10. Remove lesson file ───────────────────────────────────────────────
    try:
        os.remove(lesson_path)
        logger.info("[%s] Lesson file removed: %s", agent_name, lesson_path)
    except OSError as e:
        logger.warning("[%s] Could not remove lesson file (manual cleanup needed): %s", agent_name, e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python intelligence/update_memory.py <lesson_json_path>")
        sys.exit(1)

    _lesson_path = sys.argv[1]

    if not os.path.exists(_lesson_path):
        print(f"Error: lesson file not found: {_lesson_path}")
        sys.exit(1)

    try:
        update_memory(_lesson_path)
    except Exception as exc:
        logger.error("update_memory failed: %s", exc, exc_info=True)
        sys.exit(1)
