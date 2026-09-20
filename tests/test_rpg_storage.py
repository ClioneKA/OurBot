from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.settings import RPGSettings


class RetiredStorageMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)

    def test_legacy_storage_returns_all_items_to_backpack(self):
        with self.store.db:
            self.store.db.execute('''CREATE TABLE rpg_inventory (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(guild_id,user_id,item_id))''')
            self.store.db.execute('''CREATE TABLE rpg_storage (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                quantity INTEGER NOT NULL, PRIMARY KEY(guild_id,user_id,item_id))''')
            self.store.db.execute('''CREATE TABLE rpg_storage_equipment (
                instance_id INTEGER PRIMARY KEY, stored_at INTEGER NOT NULL)''')
            self.store.db.execute(
                "INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity) VALUES (1,10,'paint:red',2)")
            self.store.db.execute("INSERT INTO rpg_storage VALUES (1,10,'paint:red',5)")
            self.store.db.execute("INSERT INTO rpg_storage VALUES (1,10,'paint:blue',3)")

        characters = Characters(self.store, RPGSettings())

        counts = characters.inventory_counts(1, 10)
        self.assertEqual(counts['paint:red'], 7)
        self.assertEqual(counts['paint:blue'], 3)
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('rpg_storage', tables)
        self.assertNotIn('rpg_storage_equipment', tables)


if __name__ == '__main__':
    unittest.main()
