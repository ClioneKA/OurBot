import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_raid_store import RaidStore, equipment_drop_chance, food_drop_for
from core.settings import RPGSettings


class LifeSkillBalanceTests(unittest.TestCase):
    def test_fortune_and_wheel_are_relative_and_add_before_multiplying(self):
        self.assertAlmostEqual(equipment_drop_chance(0.01, {'drop_percent': 40}), 0.014)
        self.assertAlmostEqual(equipment_drop_chance(0.125, {}, {'id': 'wheel'}), 0.175)
        self.assertAlmostEqual(
            equipment_drop_chance(0.125, {'drop_percent': 40}, {'id': 'wheel'}), 0.225)
        self.assertAlmostEqual(equipment_drop_chance(0.01, {'drop_points': 5}), 0.06)

    def test_raid_food_drop_uses_quality_chance_tier_group_and_five_percent_seasoning(self):
        cases = (
            (0, '普通', 0.35, 'cooking:meat:low', 'cooking:seasoning:low'),
            (3, '精英', 0.50, 'cooking:meat:mid', 'cooking:seasoning:mid'),
            (6, '首領', 0.75, 'cooking:meat:high', 'cooking:seasoning:high'),
            (5, '傳說', 1.00, 'cooking:meat:high', 'cooking:seasoning:high'),
        )
        for tier, quality, chance, meat, seasoning in cases:
            drop = food_drop_for({'tier': tier, 'quality': quality})
            self.assertEqual((drop['chance'], drop['meat'], drop['seasoning'],
                              drop['seasoning_chance']),
                             (chance, meat, seasoning, 0.05))

    def test_food_reward_is_atomic_and_repeat_settlement_is_idempotent(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(store.close)
        Characters(store, RPGSettings())
        repo = RaidStore(store)
        policy = SimpleNamespace(victory_xp=100, victory_gold=0, drop_chance=0.0)
        raid = repo.create(1, 10, {'kind': '測試魔獸', 'strength': 1, 'tier': 6}, 0,
                           vars(policy))
        raid.update(status='running', seed=31,
                    participants=[{'id': 1, 'state': {'job': '民兵', 'level': 1}}])
        raid['food_drop']['chance'] = 1.0
        repo.save(raid)
        player = {'name': '玩家', 'team': 0, 'job': '民兵', 'user_id': 1,
                  'stats': {'HP': 100}, 'hp': 100, 'combat_stats': {}}
        enemy = {'name': '敵人', 'team': 1, 'job': '測試魔獸', 'user_id': None,
                 'stats': {'HP': 100}, 'hp': 0, 'combat_stats': {}}
        result = repo.settle(raid['id'], {'result': '勝利', 'round': 1,
                                         'fighters': [player, enemy]}, policy)
        food_item = result['rewards'][0]['food_item']
        self.assertIn(food_item, ('cooking:meat:high', 'cooking:seasoning:high'))
        self.assertEqual(Characters(store, RPGSettings()).inventory_counts(1, 1)[food_item], 1)
        replay = repo.settle(raid['id'], {'result': '勝利', 'round': 1,
                                         'fighters': [player, enemy]}, policy)
        self.assertEqual(replay['rewards'], result['rewards'])
        self.assertEqual(Characters(store, RPGSettings()).inventory_counts(1, 1)[food_item], 1)


if __name__ == '__main__':
    unittest.main()
