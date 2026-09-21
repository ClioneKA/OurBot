"""Release manifest and durable delivery state for deployment announcements."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.8–3.10
    import tomli as tomllib


@dataclass(frozen=True)
class Release:
    version: str
    title: str
    summary: str
    changes: tuple[str, ...]
    channel_name: str = "更新資訊"

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as file:
            raw = tomllib.load(file)
        allowed = {"version", "title", "summary", "changes", "channel_name"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"更新檔包含未知欄位：{', '.join(sorted(unknown))}")
        for key in ("version", "title", "summary"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"更新檔的 {key} 必須是非空白字串")
        changes = raw.get("changes")
        if (not isinstance(changes, list) or not changes
                or any(not isinstance(item, str) or not item.strip() for item in changes)):
            raise ValueError("更新檔的 changes 必須是非空白字串陣列")
        channel_name = raw.get("channel_name", "更新資訊")
        if not isinstance(channel_name, str) or not channel_name.strip() or len(channel_name) > 100:
            raise ValueError("更新檔的 channel_name 必須是 1–100 字的字串")
        release = cls(raw["version"].strip(), raw["title"].strip(), raw["summary"].strip(),
                      tuple(item.strip() for item in changes), channel_name.strip())
        if len(release.version) > 100 or len(release.title) > 256:
            raise ValueError("更新檔的版本或標題過長")
        if len(release.description) > 4096:
            raise ValueError("更新內容超過 Discord Embed 的 4096 字上限")
        return release

    @property
    def description(self):
        bullets = "\n".join(f"• {item}" for item in self.changes)
        return f"{self.summary}\n\n**更新內容**\n{bullets}"


class UpdateAnnouncementStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS update_channels (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS update_announcements (
            guild_id INTEGER NOT NULL, version TEXT NOT NULL, channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, published_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, version))""")
        self.db.commit()

    def channel_id(self, guild_id):
        row = self.db.execute(
            "SELECT channel_id FROM update_channels WHERE guild_id=?", (guild_id,)).fetchone()
        return row[0] if row else None

    def save_channel(self, guild_id, channel_id):
        with self.db:
            self.db.execute("""INSERT INTO update_channels VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id""",
                            (guild_id, channel_id))

    def was_published(self, guild_id, version):
        return self.db.execute(
            "SELECT 1 FROM update_announcements WHERE guild_id=? AND version=?",
            (guild_id, version)).fetchone() is not None

    def mark_published(self, guild_id, version, channel_id, message_id):
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO update_announcements
                (guild_id, version, channel_id, message_id, published_at)
                VALUES (?, ?, ?, ?, ?)""", (guild_id, version, channel_id, message_id,
                                             datetime.now(timezone.utc).isoformat()))

    def close(self):
        self.db.close()
