import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Set


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    source: str
    importance: int
    updated_at: str


class MemoryStore:
    """以伺服器及使用者隔離的 SQLite 長期記憶。"""

    def __init__(self, path: str, max_per_user: int = 100):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_per_user = max_per_user
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('explicit', 'auto')),
                    importance INTEGER NOT NULL DEFAULT 3
                        CHECK(importance BETWEEN 1 AND 5),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, user_id, content)
                );

                CREATE INDEX IF NOT EXISTS idx_memories_owner
                ON memories(guild_id, user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS daily_ai_usage (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(guild_id, user_id, usage_date)
                );

                CREATE TABLE IF NOT EXISTS daily_feature_usage (
                    feature TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(feature, guild_id, user_id, usage_date)
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    preferred_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS user_affinity (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0
                        CHECK(score BETWEEN -100 AND 100),
                    change_date TEXT,
                    daily_changes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(guild_id, user_id)
                );
                """
            )

    def get_affinity(self, guild_id: int, user_id: int) -> int:
        with self.lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT score FROM user_affinity
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        return int(row["score"]) if row is not None else 0

    def set_affinity(self, guild_id: int, user_id: int, score: int) -> int:
        score = max(-100, min(score, 100))
        with self.lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_affinity(guild_id, user_id, score)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    score = excluded.score,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, score),
            )
        return score

    def apply_daily_affinity_delta(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        usage_date: str,
        daily_change_limit: int,
    ) -> int:
        """在每日變動次數內調整好感度，並回傳目前分數。"""
        delta = max(-5, min(delta, 5))
        daily_change_limit = max(1, daily_change_limit)
        with self.lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT score, change_date, daily_changes FROM user_affinity
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()

            if row is None:
                score = max(-100, min(delta, 100))
                connection.execute(
                    """
                    INSERT INTO user_affinity(
                        guild_id, user_id, score, change_date, daily_changes
                    ) VALUES (?, ?, ?, ?, 1)
                    """,
                    (guild_id, user_id, score, usage_date),
                )
                return score

            changes = int(row["daily_changes"])
            if row["change_date"] != usage_date:
                changes = 0
            if changes >= daily_change_limit:
                return int(row["score"])

            score = max(-100, min(int(row["score"]) + delta, 100))
            connection.execute(
                """
                UPDATE user_affinity
                SET score = ?, change_date = ?, daily_changes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND user_id = ?
                """,
                (score, usage_date, changes + 1, guild_id, user_id),
            )
            return score

    def get_preferred_name(self, guild_id: int, user_id: int) -> Optional[str]:
        with self.lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT preferred_name FROM user_profiles
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        return str(row["preferred_name"]) if row is not None else None

    def set_preferred_name(
        self, guild_id: int, user_id: int, preferred_name: str
    ) -> bool:
        preferred_name = " ".join(preferred_name.strip().split())[:32]
        if not preferred_name:
            return False
        with self.lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_profiles(guild_id, user_id, preferred_name)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    preferred_name = excluded.preferred_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, preferred_name),
            )
        return True

    def clear_preferred_name(self, guild_id: int, user_id: int) -> bool:
        with self.lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM user_profiles WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            return cursor.rowcount > 0

    def reserve_daily_usage(
        self,
        guild_id: int,
        user_id: int,
        usage_date: str,
        daily_limit: int,
    ) -> bool:
        """原子化占用一次每日額度；額滿時不增加計數。"""
        daily_limit = max(1, daily_limit)
        with self.lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO daily_ai_usage(
                    guild_id, user_id, usage_date, request_count
                )
                VALUES (?, ?, ?, 1)
                ON CONFLICT(guild_id, user_id, usage_date) DO UPDATE SET
                    request_count = request_count + 1
                WHERE request_count < ?
                """,
                (guild_id, user_id, usage_date, daily_limit),
            )
            connection.execute(
                "DELETE FROM daily_ai_usage WHERE usage_date < date(?, '-7 days')",
                (usage_date,),
            )
            return cursor.rowcount > 0

    def reserve_daily_feature_usage(
        self,
        feature: str,
        guild_id: int,
        user_id: int,
        usage_date: str,
        user_limit: int,
        guild_limit: int,
    ) -> bool:
        """原子化占用個人與伺服器的功能額度；user_id 0 代表總額。"""
        feature = feature.strip()[:32]
        if not feature:
            return False
        user_limit = max(1, user_limit)
        guild_limit = max(1, guild_limit)

        with self.lock, self._connection() as connection:
            guild_row = connection.execute(
                """
                SELECT request_count FROM daily_feature_usage
                WHERE feature = ? AND guild_id = ? AND user_id = 0
                    AND usage_date = ?
                """,
                (feature, guild_id, usage_date),
            ).fetchone()
            user_row = connection.execute(
                """
                SELECT request_count FROM daily_feature_usage
                WHERE feature = ? AND guild_id = ? AND user_id = ?
                    AND usage_date = ?
                """,
                (feature, guild_id, user_id, usage_date),
            ).fetchone()
            if (
                guild_row is not None
                and int(guild_row["request_count"]) >= guild_limit
            ) or (
                user_row is not None
                and int(user_row["request_count"]) >= user_limit
            ):
                return False

            for counter_user_id in (0, user_id):
                connection.execute(
                    """
                    INSERT INTO daily_feature_usage(
                        feature, guild_id, user_id, usage_date, request_count
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(feature, guild_id, user_id, usage_date)
                    DO UPDATE SET request_count = request_count + 1
                    """,
                    (feature, guild_id, counter_user_id, usage_date),
                )
            connection.execute(
                """
                DELETE FROM daily_feature_usage
                WHERE usage_date < date(?, '-7 days')
                """,
                (usage_date,),
            )
            return True

    def add(
        self,
        guild_id: int,
        user_id: int,
        content: str,
        source: str = "explicit",
        importance: int = 3,
    ) -> Optional[int]:
        content = " ".join(content.strip().split())[:300]
        if not content or source not in {"explicit", "auto"}:
            return None

        importance = max(1, min(importance, 5))
        with self.lock, self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM memories
                WHERE guild_id = ? AND user_id = ? AND content = ?
                """,
                (guild_id, user_id, content),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE memories
                    SET updated_at = CURRENT_TIMESTAMP,
                        importance = MAX(importance, ?)
                    WHERE id = ?
                    """,
                    (importance, existing["id"]),
                )
                return int(existing["id"])

            count = connection.execute(
                """
                SELECT COUNT(*) AS amount FROM memories
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["amount"]
            if count >= self.max_per_user:
                oldest_auto = connection.execute(
                    """
                    SELECT id FROM memories
                    WHERE guild_id = ? AND user_id = ? AND source = 'auto'
                    ORDER BY updated_at ASC, id ASC LIMIT 1
                    """,
                    (guild_id, user_id),
                ).fetchone()
                if oldest_auto is None:
                    return None
                connection.execute(
                    "DELETE FROM memories WHERE id = ?", (oldest_auto["id"],)
                )

            cursor = connection.execute(
                """
                INSERT INTO memories(guild_id, user_id, content, source, importance)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, content, source, importance),
            )
            return int(cursor.lastrowid)

    def list_for_user(
        self, guild_id: int, user_id: int, limit: int = 20
    ) -> List[Memory]:
        with self.lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, content, source, importance, updated_at
                FROM memories
                WHERE guild_id = ? AND user_id = ?
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (guild_id, user_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [Memory(**dict(row)) for row in rows]

    def forget(self, guild_id: int, user_id: int, memory_id: int) -> bool:
        with self.lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memories
                WHERE id = ? AND guild_id = ? AND user_id = ?
                """,
                (memory_id, guild_id, user_id),
            )
            return cursor.rowcount > 0

    def update(
        self,
        guild_id: int,
        user_id: int,
        memory_id: int,
        content: str,
        importance: int = 4,
    ) -> bool:
        content = " ".join(content.strip().split())[:300]
        if not content:
            return False

        importance = max(1, min(importance, 5))
        with self.lock, self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    UPDATE memories
                    SET content = ?, importance = ?, source = 'explicit',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND guild_id = ? AND user_id = ?
                    """,
                    (content, importance, memory_id, guild_id, user_id),
                )
            except sqlite3.IntegrityError:
                return False
            return cursor.rowcount > 0

    @staticmethod
    def _search_units(text: str) -> Set[str]:
        normalized = text.casefold()
        words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
        words.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
        return words

    def search(
        self, guild_id: int, user_id: int, query: str, limit: int = 5
    ) -> List[Memory]:
        candidates = self.list_for_user(guild_id, user_id, self.max_per_user)
        query_units = self._search_units(query)

        def score(memory: Memory):
            overlap = len(query_units & self._search_units(memory.content))
            explicit_bonus = 1 if memory.source == "explicit" else 0
            return overlap * 10 + memory.importance + explicit_bonus

        candidates.sort(key=score, reverse=True)
        return candidates[: max(1, min(limit, 10))]
