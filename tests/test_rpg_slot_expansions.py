from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics
from core.rpg_character import CharacterError, Characters
from core.rpg_loadouts import Loadouts
from core.rpg_provisions import Provisions
from core.rpg_slot_expansions import EXPANSIONS, SlotExpansions
from core.settings import RPGSettings


class SlotExpansionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.expansions = self.characters.expansions
        self.provisions = Provisions(self.store)
        self.loadouts = Loadouts(self.store, self.characters, Tactics(self.store))

    def test_five_escalating_purchases_persist_and_are_isolated(self):
        self.characters.grant_item(1, 1, 'proof:raid', 400)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,40000)')
        for kind, (_, _, prices) in EXPANSIONS.items():
            for count, price in enumerate(prices):
                status = self.expansions.buy(1, 1, kind, expected_purchased=count)
                self.assertEqual(status['price'], price)
                self.assertEqual(self.expansions.capacity(1, 1, kind), 4 + count)
            with self.assertRaisesRegex(CharacterError, '上限'):
                self.expansions.buy(1, 1, kind, expected_purchased=5)
            self.assertEqual(SlotExpansions(self.store).capacity(1, 1, kind), 8)
            self.assertEqual(self.expansions.capacity(2, 1, kind), 3)
            self.assertEqual(self.expansions.capacity(1, 2, kind), 3)
        self.assertEqual(self.store.gold(1, 1), 0)
        self.assertEqual(self.characters.inventory_counts(1, 1).get('proof:raid', 0), 0)
        self.assertEqual(len(self.loadouts.all(1, 1)), 8)
        self.assertEqual(len(self.provisions.presets(1, 1)), 8)

    def test_insufficient_funds_stale_quote_and_failed_delivery_do_not_charge(self):
        for kind in EXPANSIONS:
            with self.assertRaisesRegex(CharacterError, '不足'):
                self.expansions.buy(1, 1, kind, expected_purchased=0)
            self.assertEqual(self.expansions.capacity(1, 1, kind), 3)
        self.characters.grant_item(1, 1, 'proof:raid', 60)
        self.expansions.buy(1, 1, 'expansion:loadout', expected_purchased=0)
        with self.assertRaisesRegex(CharacterError, '價格已變更'):
            self.expansions.buy(1, 1, 'expansion:loadout', expected_purchased=0)
        self.assertEqual(self.characters.inventory_counts(1, 1)['proof:raid'], 40)
        with self.store.db:
            self.store.db.execute('''CREATE TRIGGER reject_expansion BEFORE UPDATE
                ON rpg_slot_expansions BEGIN SELECT RAISE(ABORT, 'delivery failed'); END''')
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.expansions.buy(1, 1, 'expansion:loadout', expected_purchased=1)
        self.assertEqual(self.characters.inventory_counts(1, 1)['proof:raid'], 40)
        self.assertEqual(self.expansions.capacity(1, 1, 'expansion:loadout'), 4)

    def test_expanded_slots_support_save_apply_rename_and_clear(self):
        with self.assertRaises(CharacterError):
            self.loadouts.get(1, 1, 4)
        with self.assertRaises(CharacterError):
            self.provisions.preset(1, 1, 4)
        self.characters.grant_item(1, 1, 'proof:raid', 20)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,2000)')
        for kind in EXPANSIONS:
            self.expansions.buy(1, 1, kind, expected_purchased=0)
        self.store.award_voice([(1, 1, level_floor(50))])
        self.characters.change_job(1, 1, '騎士')
        self.loadouts.save(1, 1, 4)
        self.loadouts.rename(1, 1, 4, '新配置')
        self.characters.change_job(1, 1, '弓兵')
        self.assertEqual(self.loadouts.apply(1, 1, 4)['job'], '騎士')
        self.assertEqual(self.loadouts.get(1, 1, 4)['name'], '新配置')
        self.loadouts.clear(1, 1, 4)
        self.assertIsNone(self.loadouts.get(1, 1, 4)['data'])
        ingredients = ['fishing:pond:common'] * 5
        self.provisions.save_preset(1, 1, 4, ingredients)
        self.provisions.rename_preset(1, 1, 4, '新配方')
        self.assertEqual(self.provisions.preset(1, 1, 4)['ingredients'], ingredients)
        self.assertEqual(self.provisions.preset(1, 1, 4)['name'], '新配方')
        self.provisions.clear_preset(1, 1, 4)
        self.assertIsNone(self.provisions.preset(1, 1, 4)['ingredients'])
        for slot in (5, 0, -1, True, '4'):
            with self.assertRaises(CharacterError):
                self.loadouts.get(1, 1, slot)
            with self.assertRaises(CharacterError):
                self.provisions.preset(1, 1, slot)
