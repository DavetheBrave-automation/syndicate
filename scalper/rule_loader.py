"""
rule_loader.py — Hot-reload rule cache for The Syndicate Scalper.

Provides two public methods consumed by scalper_engine on the hot path:
  - get_rules(ticker)    → list[dict]  (active, non-expired rules for one ticker)
  - get_all_rules()      → dict[str, list[dict]]  (full snapshot keyed by ticker)

Rules live under {SYNDICATE_ROOT}/rules/{SCALP,SWING,POSITION}/*.json.
Each JSON file is a single rule dict.  A background daemon thread reloads
the directory tree every RELOAD_INTERVAL_SECONDS seconds.

Module-level singleton:  rule_loader = RuleLoader()
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Sys-path bootstrap — must precede any local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYNDICATE_ROOT not in sys.path:
    sys.path.insert(0, _SYNDICATE_ROOT)

logger = logging.getLogger("syndicate.rules")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELOAD_INTERVAL_SECONDS = 60

_RULE_CLASSES = ("SCALP", "SWING", "POSITION")

_REQUIRED_FIELDS = (
    "ticker",
    "class",
    "entry_price",
    "target_price",
    "stop_price",
    "max_size",
    "expiry",
)


# ---------------------------------------------------------------------------
# RuleLoader
# ---------------------------------------------------------------------------

class RuleLoader:
    """
    Loads and hot-reloads Syndicate trading rules from disk.

    Thread safety: threading.RLock() guards all reads/writes to _rules.
    The reentrant lock allows add_rule() to safely call _update_cache()
    from the same thread that already holds the lock.
    """

    def __init__(self, rules_dir: Optional[str] = None):
        self._rules_dir = rules_dir or os.path.join(_SYNDICATE_ROOT, "rules")
        self._lock = threading.RLock()

        # ticker → list[dict]  (all rules, including expired; expiry filtered at read)
        self._rules: dict[str, list[dict]] = {}

        self._last_reload: Optional[float] = None
        self._running = False
        self._reload_thread: Optional[threading.Thread] = None

        # Ensure rule subdirs exist
        for cls in _RULE_CLASSES:
            subdir = os.path.join(self._rules_dir, cls)
            os.makedirs(subdir, exist_ok=True)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self):
        """Load rules immediately, then launch background hot-reload thread."""
        self._reload()

        if self._running:
            logger.warning("[RuleLoader] start() called but already running.")
            return

        self._running = True
        self._reload_thread = threading.Thread(
            target=self._reload_loop,
            name="rule-loader-reload",
            daemon=True,
        )
        self._reload_thread.start()
        logger.info(
            "[RuleLoader] Hot-reload thread started (every %ds).",
            RELOAD_INTERVAL_SECONDS,
        )

    def stop(self):
        """Signal the reload thread to stop. Does not join."""
        self._running = False
        logger.info("[RuleLoader] Stop signalled.")

    # -----------------------------------------------------------------------
    # Public interface — hot path
    # -----------------------------------------------------------------------

    def get_rules(self, ticker: str) -> list[dict]:
        """
        Return active (non-expired) rules for a single ticker.

        HOT PATH — acquires lock for snapshot, filters expiry in-line,
        releases lock before returning.  Target: no I/O, no blocking.
        """
        with self._lock:
            candidates = list(self._rules.get(ticker, []))

        return [r for r in candidates if not self._is_expired(r)]

    def get_all_rules(self) -> dict[str, list[dict]]:
        """
        Return a full snapshot of all active (non-expired) rules keyed by ticker.

        Returns a copy — callers cannot mutate the live cache.
        """
        with self._lock:
            snapshot = {t: list(rules) for t, rules in self._rules.items()}

        result: dict[str, list[dict]] = {}
        for ticker, rules in snapshot.items():
            active = [r for r in rules if not self._is_expired(r)]
            if active:
                result[ticker] = active
        return result

    # -----------------------------------------------------------------------
    # Rule management
    # -----------------------------------------------------------------------

    def add_rule(self, rule: dict) -> bool:
        """
        Write a new rule to disk and insert it into the in-memory cache.

        Validates required fields.  File is written to:
            rules/{class}/{ticker}_{timestamp}.json

        Returns True on success, False on validation or I/O error.
        """
        # Validate required fields
        missing = [f for f in _REQUIRED_FIELDS if f not in rule]
        if missing:
            logger.error("[RuleLoader] add_rule: missing required fields: %s", missing)
            return False

        rule_class = rule["class"]
        if rule_class not in _RULE_CLASSES:
            logger.error(
                "[RuleLoader] add_rule: unknown class %r (expected one of %s)",
                rule_class, _RULE_CLASSES,
            )
            return False

        ticker = rule["ticker"]
        timestamp = int(time.time() * 1000)
        filename = f"{ticker}_{timestamp}.json"
        filepath = os.path.join(self._rules_dir, rule_class, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(rule, fh, indent=2)
        except Exception as exc:
            logger.error("[RuleLoader] add_rule: failed to write %s: %s", filepath, exc)
            return False

        # Update in-memory cache immediately (no need to wait for next reload)
        with self._lock:
            self._rules.setdefault(ticker, []).append(rule)

        logger.info(
            "[RuleLoader] Rule added: ticker=%s class=%s file=%s",
            ticker, rule_class, filename,
        )
        return True

    def remove_rule(self, rule_file: str) -> bool:
        """
        Delete a rule JSON file from disk and evict it from the in-memory cache.

        rule_file may be a bare filename (e.g. "TICKER_123.json") or an absolute
        path.  If bare, the loader searches all three class subdirs.

        Returns True on success, False if the file was not found or could not
        be removed.
        """
        filepath = self._resolve_rule_file(rule_file)
        if filepath is None:
            logger.error("[RuleLoader] remove_rule: file not found: %s", rule_file)
            return False

        # Load the rule to know its ticker before deleting
        rule = self._load_json_file(filepath)

        try:
            os.remove(filepath)
        except Exception as exc:
            logger.error("[RuleLoader] remove_rule: failed to delete %s: %s", filepath, exc)
            return False

        # Evict from cache by file identity if we have the rule data
        if rule and "ticker" in rule:
            ticker = rule["ticker"]
            with self._lock:
                existing = self._rules.get(ticker, [])
                # Remove by object equality (rule dicts from the same file load match)
                self._rules[ticker] = [r for r in existing if r != rule]
                if not self._rules[ticker]:
                    del self._rules[ticker]

        logger.info("[RuleLoader] Rule removed: %s", filepath)
        return True

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def get_stats(self) -> dict:
        """
        Return a status snapshot for scan_engine status reports.

        {
            total_rules:   int,   # active (non-expired) rules across all tickers
            by_class:      dict,  # {class: count} of active rules
            expired_count: int,   # rules in cache that are expired
            last_reload:   float  # time.time() of last successful reload, or 0.0
        }
        """
        with self._lock:
            all_rules = [r for rules in self._rules.values() for r in rules]

        active = [r for r in all_rules if not self._is_expired(r)]
        expired = [r for r in all_rules if self._is_expired(r)]

        by_class: dict[str, int] = {}
        for r in active:
            cls = r.get("class", "UNKNOWN")
            by_class[cls] = by_class.get(cls, 0) + 1

        return {
            "total_rules": len(active),
            "by_class": by_class,
            "expired_count": len(expired),
            "last_reload": self._last_reload or 0.0,
        }

    # -----------------------------------------------------------------------
    # Internal: reload loop
    # -----------------------------------------------------------------------

    def _reload_loop(self):
        """Daemon thread body. Calls _reload() every RELOAD_INTERVAL_SECONDS."""
        while self._running:
            time.sleep(RELOAD_INTERVAL_SECONDS)
            if not self._running:
                break
            try:
                self._reload()
            except Exception as exc:
                logger.error("[RuleLoader] _reload error: %s", exc, exc_info=True)

    def _reload(self):
        """
        Scan all three class subdirectories, load valid JSON files, and
        rebuild the in-memory _rules dict.

        Skips malformed JSON and rules with missing required fields (logs
        a warning for each).  Never raises — errors are logged only.
        """
        new_rules: dict[str, list[dict]] = {}
        total_loaded = 0
        total_expired = 0

        for cls in _RULE_CLASSES:
            subdir = os.path.join(self._rules_dir, cls)
            if not os.path.isdir(subdir):
                continue

            try:
                entries = os.listdir(subdir)
            except Exception as exc:
                logger.warning("[RuleLoader] Cannot list %s: %s", subdir, exc)
                continue

            for fname in entries:
                if not fname.endswith(".json"):
                    continue

                filepath = os.path.join(subdir, fname)
                rule = self._load_json_file(filepath)
                if rule is None:
                    continue  # malformed JSON — already warned in _load_json_file

                # Validate required fields
                missing = [f for f in _REQUIRED_FIELDS if f not in rule]
                if missing:
                    logger.warning(
                        "[RuleLoader] Skipping %s — missing fields: %s",
                        fname, missing,
                    )
                    continue

                ticker = rule["ticker"]
                new_rules.setdefault(ticker, []).append(rule)
                total_loaded += 1

                if self._is_expired(rule):
                    total_expired += 1

        with self._lock:
            self._rules = new_rules
            self._last_reload = time.time()

        tickers = len(new_rules)
        active = total_loaded - total_expired
        logger.info(
            "[RuleLoader] Reload complete: %d rules loaded across %d ticker(s) "
            "(%d active, %d expired).",
            total_loaded, tickers, active, total_expired,
        )

    # -----------------------------------------------------------------------
    # Internal: helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _is_expired(rule: dict) -> bool:
        """
        Return True if rule["expiry"] is in the past (UTC).

        Malformed or missing expiry is treated as non-expired so a bad
        expiry string does not silently suppress a live rule.
        """
        expiry_str = rule.get("expiry")
        if not expiry_str:
            return False
        try:
            # Accept both "Z" suffix and "+00:00" offset
            expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) >= expiry_dt
        except Exception:
            logger.warning(
                "[RuleLoader] Unparseable expiry %r for ticker %s — treating as non-expired.",
                expiry_str, rule.get("ticker", "?"),
            )
            return False

    def _load_json_file(self, filepath: str) -> Optional[dict]:
        """
        Load and parse a single JSON rule file.

        Returns the parsed dict on success, None on any error.
        Logs a warning for malformed files but never raises.
        """
        try:
            with open(filepath, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning(
                    "[RuleLoader] Skipping %s — expected a JSON object, got %s.",
                    filepath, type(data).__name__,
                )
                return None
            return data
        except json.JSONDecodeError as exc:
            logger.warning("[RuleLoader] Malformed JSON in %s: %s", filepath, exc)
            return None
        except Exception as exc:
            logger.warning("[RuleLoader] Cannot read %s: %s", filepath, exc)
            return None

    def _resolve_rule_file(self, rule_file: str) -> Optional[str]:
        """
        Resolve a rule_file argument to an absolute path.

        If rule_file is already absolute and exists, return it.
        Otherwise search across all three class subdirs.
        Returns None if the file cannot be found.
        """
        if os.path.isabs(rule_file) and os.path.isfile(rule_file):
            return rule_file

        basename = os.path.basename(rule_file)
        for cls in _RULE_CLASSES:
            candidate = os.path.join(self._rules_dir, cls, basename)
            if os.path.isfile(candidate):
                return candidate

        return None

    # -----------------------------------------------------------------------
    # Cache update helper (used internally when add_rule doesn't want to
    # trigger a full filesystem reload but does need a tidy re-index)
    # -----------------------------------------------------------------------

    def _update_cache(self, ticker: str, rule: dict):
        """Insert rule into live cache under ticker. Caller must hold _lock."""
        self._rules.setdefault(ticker, []).append(rule)


# ---------------------------------------------------------------------------
# Module-level singleton — import and call start() at application boot
# ---------------------------------------------------------------------------

rule_loader = RuleLoader()
