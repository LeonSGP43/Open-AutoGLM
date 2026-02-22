"""SQLite-backed UI state graph store for map-first navigation learning."""

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
class NavigationActionHint:
    """A candidate action learned from state-transition history."""

    action: dict[str, Any]
    attempts: int
    success_rate: float
    confidence: float
    avg_latency_ms: float
    source: str = "state_graph"
    consecutive_failures: int = 0
    last_outcome: int = 0
    updated_at: int = 0


class NavigationMapStore:
    """Store and query state transitions for map-first policy acceleration."""

    def __init__(self, db_path: str | None = None, enabled: bool | None = None):
        self.enabled = _env_flag("PHONE_AGENT_NAVIGATION_MAP_ENABLED", True) if enabled is None else enabled
        self.db_path = (
            Path(db_path).expanduser()
            if db_path
            else Path(
                os.getenv(
                    "PHONE_AGENT_NAVIGATION_DB",
                    str(Path.home() / ".openautoglm" / "navigation_map.db"),
                )
            ).expanduser()
        )
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS navigation_states (
                state_id TEXT PRIMARY KEY,
                current_app TEXT NOT NULL,
                screen_hash TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_navigation_states_app_hash
            ON navigation_states(current_app, screen_hash, last_seen)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_transitions (
                from_state_id TEXT NOT NULL,
                to_state_id TEXT NOT NULL,
                action_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                total_latency_ms INTEGER NOT NULL DEFAULT 0,
                last_outcome INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (from_state_id, to_state_id, action_json)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_state_transitions_from_state
            ON state_transitions(from_state_id, updated_at)
            """
        )
        self._conn.commit()

    @staticmethod
    def build_state_id(current_app: str, screen_hash: str) -> str:
        app = (current_app or "").strip().lower()
        shash = (screen_hash or "").strip().lower()
        if not app or not shash:
            return ""
        raw = f"{app}|{shash}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _action_to_json(action: dict[str, Any]) -> str:
        return json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def observe_state(self, current_app: str, screen_hash: str) -> str:
        """Upsert and return state_id for the current UI state."""
        if not self.enabled or self._conn is None:
            return ""
        state_id = self.build_state_id(current_app, screen_hash)
        if not state_id:
            return ""
        now_ts = int(time.time())
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO navigation_states(
                        state_id, current_app, screen_hash, first_seen, last_seen, seen_count
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(state_id)
                    DO UPDATE SET
                        last_seen = excluded.last_seen,
                        seen_count = navigation_states.seen_count + 1
                    """,
                    (state_id, current_app, screen_hash, now_ts, now_ts),
                )
                self._conn.commit()
        except sqlite3.Error:
            return ""
        return state_id

    def observe_transition(
        self,
        from_state_id: str,
        to_state_id: str,
        action: dict[str, Any],
        success: bool,
        latency_ms: int = 0,
    ) -> None:
        """Record one transition edge execution outcome."""
        if not self.enabled or self._conn is None:
            return
        if not from_state_id or not to_state_id:
            return
        if not isinstance(action, dict) or action.get("_metadata") != "do":
            return
        action_name = str(action.get("action", "") or "")
        if action_name in {"Take_over", "Interact"}:
            return

        action_json = self._action_to_json(action)
        now_ts = int(time.time())
        latency_ms = max(0, int(latency_ms))
        success_int = 1 if success else 0

        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO state_transitions(
                        from_state_id, to_state_id, action_json,
                        attempts, successes, total_latency_ms, last_outcome,
                        consecutive_failures, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(from_state_id, to_state_id, action_json)
                    DO UPDATE SET
                        attempts = state_transitions.attempts + 1,
                        successes = state_transitions.successes + excluded.successes,
                        total_latency_ms = state_transitions.total_latency_ms + excluded.total_latency_ms,
                        last_outcome = excluded.last_outcome,
                        consecutive_failures = CASE
                            WHEN excluded.last_outcome = 1 THEN 0
                            ELSE state_transitions.consecutive_failures + 1
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        from_state_id,
                        to_state_id,
                        action_json,
                        success_int,
                        latency_ms,
                        success_int,
                        0 if success else 1,
                        now_ts,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            return

    @staticmethod
    def _wilson_lower_bound(successes: int, attempts: int, z: float = 1.96) -> float:
        """Conservative lower bound of Bernoulli success probability."""
        if attempts <= 0:
            return 0.0
        p = max(0.0, min(1.0, float(successes) / float(attempts)))
        n = float(attempts)
        z2 = z * z
        denom = 1.0 + z2 / n
        center = p + z2 / (2.0 * n)
        margin = z * ((p * (1.0 - p) + z2 / (4.0 * n)) / n) ** 0.5
        return max(0.0, min(1.0, (center - margin) / denom))

    @classmethod
    def _confidence(
        cls,
        attempts: int,
        successes: int,
        consecutive_failures: int = 0,
        last_outcome: int = 0,
        updated_at: int = 0,
    ) -> float:
        if attempts <= 0:
            return 0.0
        lower = cls._wilson_lower_bound(successes, attempts)
        fail_penalty = 1.0 / (1.0 + max(0, int(consecutive_failures)) * 0.7)
        last_outcome_factor = 1.05 if int(last_outcome) == 1 else 0.88
        recency_factor = 1.0
        if updated_at > 0:
            age_sec = max(0, int(time.time()) - int(updated_at))
            recency_factor = max(0.75, min(1.0, 1.0 - (age_sec / (14 * 24 * 3600)) * 0.20))
        return max(0.0, min(1.0, lower * fail_penalty * last_outcome_factor * recency_factor))

    def get_best_action(
        self,
        from_state_id: str,
        min_attempts: int | None = None,
        min_confidence: float | None = None,
    ) -> NavigationActionHint | None:
        """Select best action candidate from current state using conservative scoring."""
        if not self.enabled or self._conn is None:
            return None
        if not from_state_id:
            return None

        required_attempts = (
            max(1, int(min_attempts))
            if min_attempts is not None
            else max(1, int(os.getenv("PHONE_AGENT_NAVIGATION_MIN_ATTEMPTS", "3")))
        )
        required_confidence = (
            max(0.0, min(1.0, float(min_confidence)))
            if min_confidence is not None
            else max(0.0, min(1.0, float(os.getenv("PHONE_AGENT_NAVIGATION_MIN_CONFIDENCE", "0.65"))))
        )
        max_consecutive_failures = max(
            0, int(os.getenv("PHONE_AGENT_NAVIGATION_MAX_CONSECUTIVE_FAILURES", "1"))
        )

        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT action_json,
                           SUM(attempts) AS attempts,
                           SUM(successes) AS successes,
                           SUM(total_latency_ms) AS total_latency_ms,
                           MAX(consecutive_failures) AS consecutive_failures,
                           MAX(last_outcome) AS last_outcome,
                           MAX(updated_at) AS updated_at
                    FROM state_transitions
                    WHERE from_state_id = ?
                    GROUP BY action_json
                    HAVING SUM(attempts) >= ? AND MAX(consecutive_failures) <= ?
                    ORDER BY (SUM(successes) * 1.0 / SUM(attempts)) DESC, SUM(attempts) DESC
                    LIMIT 8
                    """,
                    (from_state_id, required_attempts, max_consecutive_failures),
                ).fetchall()
        except sqlite3.Error:
            return None

        best_hint: NavigationActionHint | None = None
        best_conf = -1.0
        for row in rows:
            (
                action_json,
                attempts,
                successes,
                total_latency_ms,
                consecutive_failures,
                last_outcome,
                updated_at,
            ) = row
            try:
                action = json.loads(action_json)
            except Exception:
                continue
            attempts_int = int(attempts or 0)
            successes_int = int(successes or 0)
            if attempts_int <= 0:
                continue
            confidence = self._confidence(
                attempts=attempts_int,
                successes=successes_int,
                consecutive_failures=int(consecutive_failures or 0),
                last_outcome=int(last_outcome or 0),
                updated_at=int(updated_at or 0),
            )
            if confidence < required_confidence:
                continue
            success_rate = successes_int / attempts_int
            avg_latency_ms = float(total_latency_ms or 0) / attempts_int
            hint = NavigationActionHint(
                action=action,
                attempts=attempts_int,
                success_rate=success_rate,
                confidence=confidence,
                avg_latency_ms=avg_latency_ms,
                source="state_graph",
                consecutive_failures=int(consecutive_failures or 0),
                last_outcome=int(last_outcome or 0),
                updated_at=int(updated_at or 0),
            )
            if hint.confidence > best_conf:
                best_conf = hint.confidence
                best_hint = hint
        return best_hint

