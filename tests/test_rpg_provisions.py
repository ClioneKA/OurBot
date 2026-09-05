from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import CharacterError, Characters, ITEMS
from core.rpg_provisions import FOODS, POTIONS, Provisions
from core.settings import RPGSettings


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.provisions = Provisions(self.store)

    def grant(self, key, quantity=1):
        with self.store.db:
            self.store.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (1,1,?,?) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+excluded.quantity''', (key, quantity))

    def test_recipe_catalog_has_four_foods_and_seven_types_per_tier(self):
        self.assertEqual(len(FOODS), 4)
        self.assertEqual(len(POTIONS), 14)
        self.assertEqual({key.split(':')[2] for key in POTIONS},
                         {'hp', 'attack', 'defense', 'healing', 'hit', 'evasion', 'critical'})
        self.assertTrue(all(key in ITEMS for key in (*FOODS, *POTIONS)))

    def test_crafting_is_atomic(self):
        key = 'food:pond:common'
        fish, crop = FOODS[key]['ingredients']
        self.grant(fish)
        before = self.characters.inventory_counts(1, 1)
        with self.assertRaises(CharacterError):
            self.provisions.craft(1, 1, key)
        self.assertEqual(self.characters.inventory_counts(1, 1), before)
        self.grant(crop)
        self.provisions.craft(1, 1, key)
        counts = self.characters.inventory_counts(1, 1)
        self.assertNotIn(fish, counts)
        self.assertNotIn(crop, counts)
        self.assertEqual(counts[key], 1)

    def test_batch_crafting_five_all_and_insufficient_rollback(self):
        key = 'food:pond:common'
        fish, crop = FOODS[key]['ingredients']
        self.grant(fish, 8)
        self.grant(crop, 6)
        self.assertEqual(self.provisions.max_craftable(1, 1, key), 6)
        self.provisions.craft(1, 1, key, 5)
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual((counts[fish], counts[crop], counts[key]), (3, 1, 5))
        before = counts.copy()
        with self.assertRaisesRegex(CharacterError, '材料不足'):
            self.provisions.craft(1, 1, key, 2)
        self.assertEqual(self.characters.inventory_counts(1, 1), before)
        remaining = self.provisions.max_craftable(1, 1, key)
        self.provisions.craft(1, 1, key, remaining)
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual(counts[key], 6)
        self.assertEqual(self.provisions.max_craftable(1, 1, key), 0)

    def test_batch_quantity_must_be_positive_integer(self):
        for quantity in (0, -1, 1.5, True):
            with self.subTest(quantity=quantity), self.assertRaises(CharacterError):
                self.provisions.craft(1, 1, 'food:pond:common', quantity)

    def test_loadout_consumes_once_and_freezes_effect(self):
        food, potion = 'food:pond:rare', 'potion:1:attack'
        self.grant(food, 2)
        self.grant(potion)
        self.provisions.select(1, 1, 'food', food)
        self.provisions.select(1, 1, 'potion', potion)
        first = self.provisions.prepare_for_raid('raid-1', 1, [1])[1]
        self.assertEqual(first['food']['regen_rounds'], 2)
        self.assertEqual(first['potion']['amount'], 5)
        self.assertEqual(self.characters.inventory_counts(1, 1)[food], 1)
        self.assertNotIn(potion, self.characters.inventory_counts(1, 1))
        self.assertEqual(self.provisions.prepare_for_raid('raid-1', 1, [1])[1], first)
        self.assertEqual(self.characters.inventory_counts(1, 1)[food], 1)
        self.assertEqual(self.provisions.loadout(1, 1), {'food': food})

    def test_selection_requires_owned_item_and_can_be_cleared(self):
        with self.assertRaises(CharacterError):
            self.provisions.select(1, 1, 'food', 'food:lake:common')
        self.grant('food:lake:common')
        self.provisions.select(1, 1, 'food', 'food:lake:common')
        self.assertEqual(self.provisions.loadout(1, 1)['food'], 'food:lake:common')
        self.provisions.select(1, 1, 'food', None)
        self.assertEqual(self.provisions.loadout(1, 1), {})


if __name__ == '__main__':
    unittest.main()
