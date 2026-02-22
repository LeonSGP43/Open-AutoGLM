"""SQLite-backed online experience store for action selection hints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ExperienceHint:
    """One retrieved action hint from historical experiences."""

    action: dict[str, Any]
    attempts: int
    success_rate: float
    avg_reward: float
    confidence: float
    source: str  # exact | app
    consecutive_failures: int = 0
    last_outcome: int = 0
    updated_at: int = 0


class ExperienceStore:
    """Store and retrieve action outcomes for similar task/app/screen states."""

    def __init__(self, db_path: str | None = None, enabled: bool | None = None):
        self.enabled = _env_flag("PHONE_AGENT_EXPERIENCE_ENABLED", True) if enabled is None else enabled
        self.db_path = (
            Path(db_path).expanduser()
            if db_path
            else Path(
                os.getenv(
                    "PHONE_AGENT_EXPERIENCE_DB",
                    str(Path.home() / ".openautoglm" / "experience.db"),
                )
            ).expanduser()
        )
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_stats (
                task_signature TEXT NOT NULL,
                current_app TEXT NOT NULL,
                screen_hash TEXT NOT NULL,
                action_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                total_reward REAL NOT NULL DEFAULT 0.0,
                last_outcome INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (task_signature, current_app, screen_hash, action_json)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_action_stats_task_app
            ON action_stats(task_signature, current_app, updated_at)
            """
        )
        self._migrate_schema_if_needed()
        self._conn.commit()

    @staticmethod
    def normalize_task(task: str) -> str:
        """Normalize task text into a stable signature key."""
        compact = " ".join((task or "").strip().lower().split())
        return compact[:240]

    @staticmethod
    def build_screen_hash(base64_data: str, width: int, height: int) -> str:
        """Build a compact state hash from screenshot payload and geometry."""
        prefix = (base64_data or "")[:24000]
        raw = f"{width}x{height}:{prefix}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _action_to_json(action: dict[str, Any]) -> str:
        return json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _estimate_reward(
        action: dict[str, Any], success: bool, finished: bool, message: str | None
    ) -> float:
        """Estimate step reward from coarse execution outcome."""
        reward = 1.0 if success else -1.0
        if finished and success:
            reward += 0.4
        msg = (message or "").lower()
        if any(keyword in msg for keyword in ("fail", "error", "not found", "失败", "未找到")):
            reward -= 0.5
        if action.get("_metadata") == "finish" and not success:
            reward -= 0.5
        return reward

    @staticmethod
    def _confidence(
        attempts: int,
        success_rate: float,
        avg_reward: float,
        consecutive_failures: int = 0,
        last_outcome: int = 0,
        updated_at: int = 0,
    ) -> float:
        if attempts <= 0:
            return 0.0
        sample_factor = min(1.0, attempts / 6.0)
        reward_factor = max(0.0, min(1.0, (avg_reward + 1.5) / 3.0))
        score = 0.65 * success_rate + 0.35 * reward_factor
        # Penalize repeated failures and stale experience, boost recent successful feedback.
        fail_penalty = 1.0 / (1.0 + max(0, consecutive_failures) * 0.7)
        last_outcome_factor = 1.06 if int(last_outcome) == 1 else 0.90
        recency_factor = 1.0
        if updated_at > 0:
            age_sec = max(0, int(time.time()) - int(updated_at))
            # Soft half-life around 14 days.
            recency_factor = max(0.70, min(1.0, 1.0 - (age_sec / (14 * 24 * 3600)) * 0.25))
        return max(0.0, min(1.0, score * sample_factor * fail_penalty * last_outcome_factor * recency_factor))

    def _migrate_schema_if_needed(self) -> None:
        """Apply backward-compatible schema migrations."""
        if self._conn is None:
            return
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(action_stats)").fetchall()
            if len(row) > 1
        }
        if "consecutive_failures" not in columns:
            self._conn.execute(
                "ALTER TABLE action_stats ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
            )

    def observe(
        self,
        task: str,
        current_app: str,
        screen_hash: str,
        action: dict[str, Any],
        success: bool,
        finished: bool,
        message: str | None = None,
    ) -> None:
        """Record one action outcome."""
        if not self.enabled or self._conn is None:
            return
        task_signature = self.normalize_task(task)
        if not task_signature or not current_app or not screen_hash or not action:
            return

        reward = self._estimate_reward(action, success, finished, message)
        action_json = self._action_to_json(action)
        now_ts = int(time.time())

        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO action_stats(
                        task_signature, current_app, screen_hash, action_json,
                        attempts, successes, total_reward, last_outcome,
                        consecutive_failures, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_signature, current_app, screen_hash, action_json)
                    DO UPDATE SET
                        attempts = attempts + 1,
                        successes = successes + excluded.successes,
                        total_reward = total_reward + excluded.total_reward,
                        last_outcome = excluded.last_outcome,
                        consecutive_failures = CASE
                            WHEN excluded.last_outcome = 1 THEN 0
                            ELSE action_stats.consecutive_failures + 1
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task_signature,
                        current_app,
                        screen_hash,
                        action_json,
                        1 if success else 0,
                        reward,
                        1 if success else 0,
                        0 if success else 1,
                        now_ts,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            return

    def get_hint(
        self,
        task: str,
        current_app: str,
        screen_hash: str,
        min_attempts: int | None = None,
    ) -> ExperienceHint | None:
        """Retrieve best historical action hint for current context."""
        if not self.enabled or self._conn is None:
            return None
        task_signature = self.normalize_task(task)
        if not task_signature or not current_app:
            return None

        required_attempts = min_attempts
        if required_attempts is None:
            required_attempts = max(1, int(os.getenv("PHONE_AGENT_EXPERIENCE_MIN_ATTEMPTS", "2")))
        max_consecutive_failures = max(
            0, int(os.getenv("PHONE_AGENT_EXPERIENCE_MAX_CONSECUTIVE_FAILURES", "2"))
        )

        try:
            with self._lock:
                exact = self._conn.execute(
                    """
                    SELECT action_json, attempts, successes, total_reward,
                           consecutive_failures, last_outcome, updated_at
                    FROM action_stats
                    WHERE task_signature = ?
                      AND current_app = ?
                      AND screen_hash = ?
                      AND attempts >= ?
                      AND consecutive_failures <= ?
                    ORDER BY (total_reward * 1.0 / attempts) DESC, successes DESC, attempts DESC
                    LIMIT 1
                    """,
                    (
                        task_signature,
                        current_app,
                        screen_hash,
                        required_attempts,
                        max_consecutive_failures,
                    ),
                ).fetchone()
                if exact:
                    return self._build_hint(exact, source="exact")

                app_level = self._conn.execute(
                    """
                    SELECT action_json,
                           SUM(attempts) AS attempts,
                           SUM(successes) AS successes,
                           SUM(total_reward) AS total_reward,
                           MAX(consecutive_failures) AS consecutive_failures,
                           MAX(last_outcome) AS last_outcome,
                           MAX(updated_at) AS updated_at
                    FROM action_stats
                    WHERE task_signature = ? AND current_app = ?
                    GROUP BY action_json
                    HAVING SUM(attempts) >= ? AND MAX(consecutive_failures) <= ?
                    ORDER BY (total_reward * 1.0 / attempts) DESC, successes DESC, attempts DESC
                    LIMIT 1
                    """,
                    (task_signature, current_app, required_attempts, max_consecutive_failures),
                ).fetchone()
                if app_level:
                    return self._build_hint(app_level, source="app")
        except sqlite3.Error:
            return None
        return None

    def is_action_in_cooldown(
        self,
        task: str,
        current_app: str,
        screen_hash: str,
        action: dict[str, Any],
    ) -> bool:
        """Return True when a recently failed action should be temporarily blocked."""
        if not self.enabled or self._conn is None:
            return False
        cooldown_sec = max(0, int(os.getenv("PHONE_AGENT_EXPERIENCE_FAILURE_COOLDOWN_SEC", "120")))
        min_failures = max(1, int(os.getenv("PHONE_AGENT_EXPERIENCE_FAILURE_COOLDOWN_MIN", "2")))
        if cooldown_sec <= 0:
            return False
        task_signature = self.normalize_task(task)
        if not task_signature or not current_app or not screen_hash or not action:
            return False
        action_json = self._action_to_json(action)

        try:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT consecutive_failures, last_outcome, updated_at
                    FROM action_stats
                    WHERE task_signature = ? AND current_app = ? AND screen_hash = ? AND action_json = ?
                    LIMIT 1
                    """,
                    (task_signature, current_app, screen_hash, action_json),
                ).fetchone()
        except sqlite3.Error:
            return False
        if not row:
            return False
        consecutive_failures, last_outcome, updated_at = row
        if int(last_outcome or 0) == 1:
            return False
        if int(consecutive_failures or 0) < min_failures:
            return False
        age_sec = max(0, int(time.time()) - int(updated_at or 0))
        return age_sec <= cooldown_sec

    def _build_hint(self, row: tuple[Any, ...], source: str) -> ExperienceHint | None:
        (
            action_json,
            attempts,
            successes,
            total_reward,
            consecutive_failures,
            last_outcome,
            updated_at,
        ) = row
        if not action_json:
            return None
        try:
            action = json.loads(action_json)
        except Exception:
            return None
        if (
            isinstance(action, dict)
            and action.get("_metadata") == "do"
            and str(action.get("action", "") or "") in {"Take_over", "Interact"}
        ):
            return None
        attempts_int = int(attempts or 0)
        if attempts_int <= 0:
            return None
        successes_int = int(successes or 0)
        total_reward_float = float(total_reward or 0.0)
        success_rate = successes_int / attempts_int
        avg_reward = total_reward_float / attempts_int
        consecutive_failures_int = int(consecutive_failures or 0)
        last_outcome_int = int(last_outcome or 0)
        updated_at_int = int(updated_at or 0)
        confidence = self._confidence(
            attempts_int,
            success_rate,
            avg_reward,
            consecutive_failures=consecutive_failures_int,
            last_outcome=last_outcome_int,
            updated_at=updated_at_int,
        )
        min_confidence = float(os.getenv("PHONE_AGENT_EXPERIENCE_MIN_CONFIDENCE", "0.15"))
        if confidence < min_confidence:
            return None
        return ExperienceHint(
            action=action,
            attempts=attempts_int,
            success_rate=success_rate,
            avg_reward=avg_reward,
            confidence=confidence,
            source=source,
            consecutive_failures=consecutive_failures_int,
            last_outcome=last_outcome_int,
            updated_at=updated_at_int,
        )
