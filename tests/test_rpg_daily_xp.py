from pathlib import Path
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest

from core.rpg import RPGStore


class DailyXPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 9, 4, 15, 55, tzinfo=timezone.utc).timestamp()

    def test_caps_cooldown_partial_awards_and_server_separation(self):
        for offset in (0, 10, 60, 120):
            self.store.award_text(1, 1, self.now + offset, 15, 60, 20)
        self.assertEqual(self.store.xp(1, 1), 20)
        self.assertEqual(self.store.daily_xp(1, 1, 'text', self.now), 20)
        self.store.award_voice([(1, 1, 15), (1, 1, 15)], 25, self.now)
        self.assertEqual(self.store.xp(1, 1), 45)
        self.assertEqual(self.store.daily_xp(1, 1, 'voice', self.now), 25)
        self.store.award_text(2, 1, self.now, 15, 60, 20)
        self.assertEqual(self.store.xp(2, 1), 15)
        # Raid XP writes directly to players, without consuming either budget.
        with self.store.db:
            self.store.db.execute('UPDATE players SET xp=xp+300 WHERE guild_id=1 AND user_id=1')
        self.assertEqual(self.store.daily_xp(1, 1, 'text', self.now), 20)
        self.store.award_voice([(1, 2, 100)], 0, self.now)
        self.assertEqual(self.store.xp(1, 2), 0)

    def test_midnight_restart_and_transaction_rollback(self):
        midnight = self.now + 300
        self.store.award_text(1, 1, midnight - 60, 15, 60, 15)
        other = RPGStore(self.path)
        try:
            other.award_text(1, 1, midnight - 1, 15, 60, 15)
            self.assertEqual(other.xp(1, 1), 15)
            other.award_text(1, 1, midnight, 15, 60, 15)
            self.assertEqual(other.xp(1, 1), 30)
            self.assertEqual(other.daily_xp(1, 1, 'text', midnight), 15)
        finally:
            other.close()
        self.store.db.execute("CREATE TEMP TRIGGER reject_xp BEFORE INSERT ON players BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.award_voice([(1, 2, 10)], 100, midnight)
        self.assertEqual(self.store.daily_xp(1, 2, 'voice', midnight), 0)
        self.assertEqual(self.store.xp(1, 2), 0)
