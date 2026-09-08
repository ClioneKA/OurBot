from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import CharacterError, Characters, ITEMS
from core.rpg_provisions import (AFTERTASTE, ASSAULT, COOKING_XP_PER_QUALITY,
                                 FEAST, FORTUNE, GROWTH,
                                 INGREDIENTS, NOURISHMENT, PAIRINGS, Provisions,
                                 VITALITY, evaluate_ingredients)
from core.settings import RPGSettings


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.provisions = Provisions(self.store)

    def grant(self, key, quantity=1, user=1):
        with self.store.db:
            self.store.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (1,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+excluded.quantity''', (user, key, quantity))

    def test_all_fish_crops_weeds_and_herbs_are_cooking_ingredients(self):
        self.assertEqual(len(INGREDIENTS), 18)
        tags = {ingredient.tag for ingredient in INGREDIENTS.values()}
        self.assertEqual(tags, {GROWTH, ASSAULT, VITALITY, FEAST, NOURISHMENT, FORTUNE})
        rare = [key for key, ingredient in INGREDIENTS.items() if ingredient.aftertaste]
        self.assertEqual(rare, ['fishing:pond:rare', 'fishing:lake:rare',
                                'fishing:waterway:rare'])
        self.assertEqual(sum(INGREDIENTS[key].aftertaste for key in rare), 3)

    def test_backpack_descriptions_share_cooking_metadata_format(self):
        for key, ingredient in INGREDIENTS.items():
            description = ITEMS[key].description
            self.assertIn(f'料理標籤：{ingredient.tag}', description)
            self.assertIn(f'品質：{ingredient.quality}', description)
            expected_echo = f'+{ingredient.aftertaste}' if ingredient.aftertaste else '—'
            self.assertIn(f'餘韻：{expected_echo}', description)
            self.assertIn('推薦搭配：', description)
        for left, right in PAIRINGS:
            self.assertIn(ITEMS[right].name, ITEMS[left].description)
            self.assertIn(ITEMS[left].name, ITEMS[right].description)

    def test_evaluation_uses_quality_diversity_pairings_feast_and_aftertaste(self):
        ingredients = ['fishing:pond:rare', 'fishing:lake:rare',
                       'fishing:pond:common', 'fishing:waterway:common', 'farming:potato']
        data = evaluate_ingredients(ingredients, cooking_level=20)
        self.assertEqual(data['primary_tag'], FORTUNE)
        self.assertEqual(data['secondary_tag'], GROWTH)
        self.assertEqual(data['duration'], 2)
        self.assertEqual(data['total_portions'], 5)
        self.assertEqual(data['capacity'], 2)
        self.assertEqual(data['pairings'], 1)
        self.assertIn('drop_points', data['effect'])
        self.assertIn('xp_percent', data['effect'])

    def test_five_feast_ingredients_require_an_effect_ingredient(self):
        with self.assertRaisesRegex(CharacterError, '至少需要'):
            evaluate_ingredients(['farming:potato'] * 5)

    def test_cooking_xp_scales_from_250_to_1400(self):
        low = evaluate_ingredients(['fishing:pond:common'] * 5)
        high = evaluate_ingredients(
            ['fishing:waterway:rare'] * 4 + ['farming:moonwhite_rice'])
        self.assertEqual(COOKING_XP_PER_QUALITY, 50)
        self.assertEqual((low['grade'], low['cooking_xp']), ('C', 250))
        self.assertEqual((high['grade'], high['cooking_xp']), ('S', 1400))

    def test_cooking_is_atomic_awards_xp_and_seats_cook(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        before = self.characters.inventory_counts(1, 1)
        with self.assertRaises(CharacterError):
            self.provisions.cook(1, 1, 9, ingredients + ['farming:potato'], now=100)
        self.assertEqual(self.characters.inventory_counts(1, 1), before)

        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        data = meal['data']
        self.assertGreater(data['cooking_xp'], 0)
        self.assertEqual(self.provisions.state(1, 1)['xp'], data['cooking_xp'])
        self.assertNotIn('fishing:pond:common', self.characters.inventory_counts(1, 1))
        self.assertEqual(self.provisions.claimants(meal['id']), [1])

    def test_meal_claims_are_idempotent_and_temperance_preserves_charge(self):
        ingredients = ['fishing:pond:rare', 'fishing:lake:rare',
                       'fishing:pond:common', 'farming:potato', 'farming:wheat']
        for key in ingredients:
            self.grant(key)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.publish(meal['id'], 99)
        self.provisions.claim(meal['id'], 1, 2, now=101)

        first = self.provisions.prepare_for_raid('raid-a', 1, [2], preserve_users=[2], now=102)
        self.assertEqual(first[2]['kind'], 'meal')
        self.assertEqual(self.provisions.prepare_for_raid(
            'raid-a', 1, [2], preserve_users=[2], now=102), first)
        remaining = self.store.db.execute(
            'SELECT remaining FROM rpg_meal_claims WHERE meal_id=? AND user_id=2',
            (meal['id'],)).fetchone()[0]
        self.assertEqual(remaining, 2)

        self.provisions.prepare_for_raid('raid-b', 1, [2], now=103)
        remaining = self.store.db.execute(
            'SELECT remaining FROM rpg_meal_claims WHERE meal_id=? AND user_id=2',
            (meal['id'],)).fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_failed_publish_refunds_ingredients_and_xp(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.cancel(meal['id'], refund=True)
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual((counts['fishing:pond:common'], counts['farming:potato']), (3, 2))
        self.assertEqual(self.provisions.state(1, 1)['xp'], 0)

    def test_legacy_food_and_potions_are_refunded_once(self):
        # Simulate an older database by removing the migration marker.
        with self.store.db:
            self.store.db.execute("DELETE FROM rpg_schema_migrations WHERE name='freeform_cooking_v1'")
            self.store.db.execute('INSERT INTO rpg_inventory VALUES (1,2,?,?)',
                                  ('food:pond:common', 2))
            self.store.db.execute('INSERT INTO rpg_inventory VALUES (1,2,?,?)',
                                  ('potion:1:attack', 3))
        Provisions(self.store)
        counts = self.characters.inventory_counts(1, 2)
        self.assertEqual(counts['fishing:pond:common'], 2)
        self.assertEqual(counts['farming:potato'], 2)
        self.assertEqual(counts['fishing:pond:weed'], 3)
        self.assertEqual(counts['farming:dew_herb'], 3)
        self.assertNotIn('food:pond:common', counts)
        self.assertNotIn('potion:1:attack', counts)
        Provisions(self.store)
        self.assertEqual(self.characters.inventory_counts(1, 2), counts)


if __name__ == '__main__':
    unittest.main()
