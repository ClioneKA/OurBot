from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import CharacterError, Characters, ITEMS
from core.rpg_provisions import (AFTERTASTE, ASSAULT, COOKING_INITIAL_XP_PERCENT,
                                 COOKING_PRESET_SLOTS, COOKING_XP_PER_QUALITY, DONATION_XP_PERCENT,
                                 FEAST, FORTUNE, GROWTH,
                                 INGREDIENTS, MEAL_CLAIM_SECONDS,
                                 MEAL_EFFECT_SECONDS, NOURISHMENT,
                                 PAIRINGS, Provisions,
                                 VITALITY, evaluate_ingredients, grade_cap, secondary_percent,
                                 guest_reward_target, _grade)
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
        self.assertEqual(len(INGREDIENTS), 34)
        tags = {ingredient.tag for ingredient in INGREDIENTS.values()}
        self.assertEqual(tags, {None, GROWTH, ASSAULT, VITALITY, FEAST, NOURISHMENT, FORTUNE})
        rare = [key for key, ingredient in INGREDIENTS.items() if ingredient.seasoning]
        self.assertEqual(rare, ['cooking:seasoning:low', 'cooking:seasoning:mid',
                                'cooking:seasoning:high'])
        self.assertEqual([INGREDIENTS[key].aftertaste for key in rare], [2, 3, 4])

    def test_backpack_descriptions_share_cooking_metadata_format(self):
        for key, ingredient in INGREDIENTS.items():
            description = ITEMS[key].description
            if ingredient.seasoning:
                self.assertIn(f'美味度 +{ingredient.score_bonus}', description)
                self.assertIn(f'餘韻：+{ingredient.aftertaste}', description)
                self.assertIn('每桌最多一份', description)
                continue
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
        self.assertEqual(data['duration'], 1)
        self.assertEqual(data['total_portions'], 5)
        self.assertEqual(data['capacity'], 5)
        self.assertEqual(data['pairings'], 1)
        self.assertEqual(data['effect']['drop_percent'], 30)
        self.assertIn('xp_percent', data['effect'])

    def test_five_feast_ingredients_require_an_effect_ingredient(self):
        with self.assertRaisesRegex(CharacterError, '至少需要'):
            evaluate_ingredients(['farming:potato'] * 5)

    def test_cooking_xp_scales_from_250_to_1400(self):
        low = evaluate_ingredients(['fishing:pond:common'] * 5)
        high = evaluate_ingredients(
            ['fishing:waterway:rare'] * 4 + ['farming:moonwhite_rice'])
        self.assertEqual(COOKING_XP_PER_QUALITY, 50)
        self.assertEqual((low['grade'], low['cooking_xp']), ('D', 200))
        self.assertEqual((high['grade'], high['cooking_xp']), ('S', 1400))
        self.assertEqual(COOKING_INITIAL_XP_PERCENT, 25)
        self.assertEqual((low['initial_cooking_xp'], high['initial_cooking_xp']),
                         (50, 350))

    def test_donation_awards_half_of_ingredient_base_xp(self):
        ingredients = ['fishing:pond:common'] * 5
        self.grant('fishing:pond:common', 10)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.assertEqual((meal['data']['grade'], meal['data']['initial_cooking_xp']),
                         ('D', 50))
        self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:pond:common'], 5)

        result = self.provisions.donate(1, 1, ingredients)
        self.assertEqual(DONATION_XP_PERCENT, 50)
        self.assertEqual(result, {'quantity': 5, 'quality': 5, 'xp': 125})
        self.assertEqual(self.provisions.state(1, 1)['xp'], 175)
        self.assertNotIn('fishing:pond:common', self.characters.inventory_counts(1, 1))

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
        self.assertEqual((MEAL_CLAIM_SECONDS, meal['expires_at']), (1800, 1900))
        valid_until = self.store.db.execute(
            'SELECT valid_until FROM rpg_meal_claims WHERE meal_id=? AND user_id=1',
            (meal['id'],)).fetchone()[0]
        self.assertEqual((MEAL_EFFECT_SECONDS, valid_until), (86400, 86500))
        self.assertGreater(data['cooking_xp'], 0)
        self.assertEqual(self.provisions.state(1, 1)['xp'], data['initial_cooking_xp'])
        self.assertNotIn('fishing:pond:common', self.characters.inventory_counts(1, 1))
        self.assertEqual(self.provisions.claimants(meal['id']), [1])
        active = self.provisions.active_effect(1, 1, now=101)
        self.assertEqual((active['name'], active['remaining'], active['valid_until']),
                         (data['name'], data['duration'], 86500))
        self.assertIsNone(self.provisions.active_effect(1, 1, now=86500))

    def test_public_meals_split_remaining_xp_across_needed_guests(self):
        two_seat = ['fishing:pond:rare', 'fishing:lake:rare',
                    'fishing:pond:common', 'fishing:waterway:common', 'farming:potato']
        for key in two_seat:
            self.grant(key, user=10)
        meal = self.provisions.cook(1, 10, 9, two_seat, now=100)
        self.assertEqual((meal['capacity'], guest_reward_target(meal['capacity'])), (5, 3))
        self.provisions.publish(meal['id'], 98)
        for user in (11, 12, 13):
            self.provisions.claim(meal['id'], 1, user, now=100 + user)
        self.assertEqual(self.provisions.state(1, 10)['xp'], meal['data']['cooking_xp'])

        three_seat = ['fishing:pond:common'] * 5
        self.grant('fishing:pond:common', 5, user=20)
        meal = self.provisions.cook(1, 20, 9, three_seat, now=200)
        self.assertEqual((meal['capacity'], guest_reward_target(meal['capacity'])), (3, 2))
        self.provisions.publish(meal['id'], 99)
        full_xp = meal['data']['cooking_xp']
        initial_xp = meal['data']['initial_cooking_xp']
        remaining_xp = full_xp - initial_xp
        self.provisions.claim(meal['id'], 1, 21, now=201)
        self.assertEqual(self.provisions.state(1, 20)['xp'], initial_xp + remaining_xp // 2)
        self.provisions.claim(meal['id'], 1, 22, now=202)
        self.assertEqual(self.provisions.state(1, 20)['xp'], full_xp)

    def test_feast_seats_above_four_still_cap_xp_at_three_guests(self):
        ingredients = ['farming:potato', 'farming:wheat', 'farming:moonwhite_rice',
                       'farming:potato', 'fishing:pond:common']
        for key in ingredients:
            self.grant(key)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.assertEqual((meal['capacity'], guest_reward_target(meal['capacity'])), (8, 3))
        self.provisions.publish(meal['id'], 99)
        full_xp = meal['data']['cooking_xp']
        initial_xp = meal['data']['initial_cooking_xp']
        remaining_xp = full_xp - initial_xp
        self.assertEqual(self.provisions.state(1, 1)['xp'], initial_xp)

        for index, user in enumerate((2, 3, 4), 2):
            self.provisions.claim(meal['id'], 1, user, now=100 + user)
            rewarded_guests = index - 1
            self.assertEqual(self.provisions.state(1, 1)['xp'],
                             initial_xp + remaining_xp * rewarded_guests // 3)
        self.provisions.claim(meal['id'], 1, 5, now=105)
        self.assertEqual(self.provisions.state(1, 1)['xp'], full_xp)

    def test_aftertaste_only_changes_duration_not_seats(self):
        base = ['fishing:pond:rare', 'fishing:lake:rare',
                'fishing:pond:common', 'farming:potato']
        low = evaluate_ingredients(base + ['cooking:seasoning:low'], cooking_level=20)
        high = evaluate_ingredients(base + ['cooking:seasoning:high'], cooking_level=80)
        self.assertEqual((low['duration'], low['capacity']), (2, low['total_portions']))
        self.assertEqual((high['duration'], high['capacity']), (3, high['total_portions']))
        with self.assertRaisesRegex(CharacterError, '最多只能使用一份調味料'):
            evaluate_ingredients(base[:3] + ['cooking:seasoning:low',
                                             'cooking:seasoning:mid'])

    def test_grade_caps_and_secondary_strength_milestones(self):
        recipe = ['fishing:bay:rare', 'farming:ember_ginger', 'fishing:bay:common',
                  'farming:star_bean', 'cooking:seasoning:high']
        level_one = evaluate_ingredients(recipe, cooking_level=1)
        level_forty = evaluate_ingredients(recipe, cooking_level=40)
        level_eighty = evaluate_ingredients(recipe, cooking_level=80)
        self.assertEqual((level_one['potential_grade'], level_one['grade']), ('SS', 'S'))
        self.assertEqual((level_forty['potential_grade'], level_forty['grade']), ('SSS', 'SS'))
        self.assertEqual((level_eighty['potential_grade'], level_eighty['grade']), ('SSS', 'SSS'))
        self.assertEqual([grade_cap(level) for level in (1, 40, 80)], ['S', 'SS', 'SSS'])
        self.assertEqual([secondary_percent(level) for level in (19, 20, 40, 60, 80, 100, 120)],
                         [0, 50, 60, 70, 80, 90, 100])

    def test_all_grade_boundaries_and_score_cap(self):
        self.assertEqual([_grade(score) for score in
                          (44, 45, 54, 55, 69, 70, 84, 85, 99, 100, 114, 115)],
                         ['D', 'C', 'C', 'B', 'B', 'A', 'A', 'S', 'S', 'SS', 'SS', 'SSS'])
        recipe = ['fishing:bay:rare', 'farming:ember_ginger', 'fishing:bay:common',
                  'farming:star_bean', 'cooking:seasoning:high']
        self.assertLessEqual(evaluate_ingredients(recipe, cooking_level=120)['score'], 130)

    def test_last_recipe_only_returns_successfully_completed_meal(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 6)
        self.grant('farming:potato', 4)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.assertIsNone(self.provisions.last_recipe(1, 1))

        self.provisions.publish(meal['id'], 99)
        self.assertEqual(self.provisions.last_recipe(1, 1), ingredients)

        donated = ['fishing:pond:common', 'farming:potato']
        self.provisions.donate(1, 1, donated)
        self.assertEqual(self.provisions.last_recipe(1, 1), donated)

    def test_cooking_presets_can_be_saved_named_and_cleared(self):
        recipe = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.assertEqual(COOKING_PRESET_SLOTS, 3)
        self.assertEqual(
            [preset['name'] for preset in self.provisions.presets(1, 1)],
            ['配方 1', '配方 2', '配方 3'])

        saved = self.provisions.save_preset(1, 1, 2, recipe)
        self.assertEqual(saved['ingredients'], recipe)
        renamed = self.provisions.rename_preset(1, 1, 2, '  成長   宴席  ')
        self.assertEqual((renamed['name'], renamed['ingredients']), ('成長 宴席', recipe))

        with self.assertRaisesRegex(CharacterError, '恰好 5'):
            self.provisions.save_preset(1, 1, 1, recipe[:2])
        with self.assertRaisesRegex(CharacterError, '最多 20'):
            self.provisions.rename_preset(
                1, 1, 1, '這是一個超過二十個中文字的料理配方名稱不能保存')

        self.provisions.clear_preset(1, 1, 2)
        self.assertEqual(self.provisions.preset(1, 1, 2),
                         {'slot': 2, 'name': '配方 2', 'ingredients': None})

    def test_cook_allows_next_table_when_previous_one_is_full_or_expired(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 9)
        self.grant('farming:potato', 6)
        first = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.publish(first['id'], 99)
        with self.assertRaisesRegex(CharacterError, '尚未客滿'):
            self.provisions.cook(1, 1, 9, ingredients, now=101)

        for user in (2, 3, 4, 5):
            self.provisions.claim(first['id'], 1, user, now=100 + user)
        self.assertEqual(len(self.provisions.claimants(first['id'])), first['capacity'])

        second = self.provisions.cook(1, 1, 9, ingredients, now=106)
        self.assertNotEqual(first['id'], second['id'])
        self.provisions.publish(second['id'], 100)
        with self.assertRaisesRegex(CharacterError, '尚未客滿'):
            self.provisions.cook(1, 1, 9, ingredients, now=107)

        third = self.provisions.cook(1, 1, 9, ingredients, now=1906)
        self.assertNotEqual(second['id'], third['id'])

    def test_meal_claims_are_idempotent_and_temperance_preserves_charge(self):
        ingredients = ['fishing:pond:rare', 'fishing:lake:rare',
                       'fishing:pond:common', 'farming:potato', 'cooking:seasoning:low']
        for key in ingredients:
            self.grant(key)
        meal = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.publish(meal['id'], 99)
        self.provisions.claim(meal['id'], 1, 2, now=101)
        with self.assertRaisesRegex(CharacterError, '料理效果'):
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
        self.assertEqual(self.provisions.active_effect(1, 2, now=103)['remaining'], 1)

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
