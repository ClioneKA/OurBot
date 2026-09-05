import json
import unittest

from core.rpg_battle import Fighter, Rule, default_rules
from core.rpg_total_battle import (
    ACTION_ATTACK,
    ACTION_SKILL,
    MAX_TOTAL_RAID_PLAYERS,
    TotalRaidBattle,
    TotalRaidError,
    dump_total_battle,
    load_total_battle,
    training_dummy_battle,
)


def player(user_id, name=None, job='民兵', hp=300, attack=80, dex=30, rules=None):
    return Fighter(
        name or f'玩家{user_id}', 0, job,
        {'HP': hp, '攻擊': attack, '防禦': 20, '治療量': 100,
         '命中率': 150, '閃避率': 0, '暴擊率': 0},
        dex, default_rules(job) if rules is None else rules,
        user_id=user_id,
    )


class TotalRaidBattleTests(unittest.TestCase):
    def test_supports_one_to_six_unique_players(self):
        for count in range(1, MAX_TOTAL_RAID_PLAYERS + 1):
            battle = training_dummy_battle([player(index) for index in range(1, count + 1)])
            self.assertEqual(len(battle.living_player_ids()), count)
        with self.assertRaises(TotalRaidError):
            training_dummy_battle([player(index) for index in range(1, 8)])
        with self.assertRaises(TotalRaidError):
            training_dummy_battle([player(1), player(1)])

    def test_requires_all_living_players_then_resolves_by_speed(self):
        first, second = player(1, dex=50), player(2, dex=40)
        battle = training_dummy_battle([first, second], seed=2)
        enemy_key = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_ATTACK, enemy_key)
        self.assertFalse(battle.ready_to_resolve())
        with self.assertRaisesRegex(TotalRaidError, '尚有 1 名'):
            battle.resolve()
        battle.submit(2, ACTION_ATTACK, enemy_key)
        self.assertTrue(battle.ready_to_resolve())
        battle.resolve()
        self.assertEqual(battle.round, 1)
        first_log = next(line for line in battle.log if '使用普通攻擊' in line)
        self.assertIn('玩家1', first_log)
        self.assertEqual(battle.choices, {})

    def test_choice_can_be_replaced_and_timeout_defaults_to_attack(self):
        first, second = player(1), player(2)
        battle = training_dummy_battle([first, second], seed=1)
        target = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_SKILL, target, 1)
        replaced = battle.submit(1, ACTION_ATTACK, target)
        self.assertEqual(replaced.action, ACTION_ATTACK)
        defaults = battle.fill_defaults()
        self.assertEqual([choice.user_id for choice in defaults], [2])
        self.assertTrue(defaults[0].automatic)
        battle.resolve()
        self.assertTrue(any('未及時選擇' in line for line in battle.log))

    def test_manual_skill_sets_and_enforces_cooldown(self):
        fighter = player(1, rules=[Rule(1, 1, True, 'always', 'lowest')])
        battle = training_dummy_battle([fighter], seed=1)
        target = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_SKILL, target, 1)
        battle.resolve()
        self.assertEqual(fighter.ready[1], 4)
        with self.assertRaisesRegex(TotalRaidError, '仍需等待 2 回合'):
            battle.submit(1, ACTION_SKILL, target, 1)
        available = battle.available_actions(1)
        self.assertEqual(available[1]['cooldown_remaining'], 2)

    def test_target_validation_and_dead_target_falls_back(self):
        first, second = player(1, attack=1000, dex=100), player(2, attack=50, dex=50)
        weak = Fighter('小假人', 1, '訓練用假人',
                       {'HP': 50, '攻擊': 1, '防禦': 0, '治療量': 0,
                        '命中率': 100, '閃避率': 0, '暴擊率': 0},
                       1, [], user_id=-1)
        sturdy = Fighter('大假人', 1, '木樁',
                         {'HP': 500, '攻擊': 1, '防禦': 0, '治療量': 0,
                          '命中率': 100, '閃避率': 0, '暴擊率': 0},
                         1, [], user_id=-2)
        battle = TotalRaidBattle([first, second, weak, sturdy], seed=1)
        weak_key = battle.key(weak)
        with self.assertRaises(TotalRaidError):
            battle.submit(1, ACTION_ATTACK, battle.key(first))
        battle.submit(1, ACTION_ATTACK, weak_key)
        battle.submit(2, ACTION_ATTACK, weak_key)
        battle.resolve()
        self.assertEqual(weak.hp, 0)
        self.assertLess(sturdy.hp, sturdy.stats['HP'])

    def test_identical_enemies_have_distinct_stable_target_keys(self):
        adventurer = player(1)
        stats = {'HP': 100, '攻擊': 1, '防禦': 0, '治療量': 0,
                 '命中率': 100, '閃避率': 0, '暴擊率': 0}
        first = Fighter('木樁', 1, '木樁', stats, 1, [])
        second = Fighter('木樁', 1, '木樁', stats, 1, [])
        battle = TotalRaidBattle([adventurer, first, second])
        self.assertNotEqual(battle.key(first), battle.key(second))
        self.assertIs(battle.fighter_for_key(battle.key(second)), second)
        self.assertIsNone(battle.fighter_for_key('e:-1'))

    def test_dummy_repeats_four_announced_fixed_actions(self):
        battle = training_dummy_battle([player(1, hp=5000, attack=1)], seed=1)
        names = []
        for _ in range(4):
            intent = battle.intent()
            names.append(intent.name)
            target = battle.key(battle.living(1)[0])
            battle.submit(1, ACTION_ATTACK, target)
            battle.resolve()
        self.assertEqual(names, ['標準打擊', '防禦校準', '廣域震波', '過載重擊'])
        self.assertTrue(any('受到的傷害' not in line and '防禦校準' in line for line in battle.log))

    def test_snapshot_preserves_pending_choices_rng_and_intent(self):
        battle = training_dummy_battle([player(1), player(2)], seed=9)
        target = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_ATTACK, target)
        data = json.loads(json.dumps(dump_total_battle(battle)))
        restored = load_total_battle(data)
        self.assertEqual(restored.choices[1], battle.choices[1])
        self.assertEqual(restored.intent(), battle.intent())
        restored.submit(2, ACTION_ATTACK, target)
        battle.submit(2, ACTION_ATTACK, target)
        restored.resolve()
        battle.resolve()
        self.assertEqual(dump_total_battle(restored), dump_total_battle(battle))

    def test_player_poison_skill_ticks_on_dummy_turn(self):
        archer = player(1, job='弓兵', rules=[Rule(1, 1, True, 'always', 'lowest', skill_id=5)])
        battle = training_dummy_battle([archer], seed=3)
        target = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_SKILL, target, 1)
        battle.resolve()
        dummy = battle.living(1)[0]
        after_hit = dummy.hp
        battle.submit(1, ACTION_ATTACK, target)
        battle.resolve()
        self.assertLess(dummy.hp, after_hit - 1)
        self.assertTrue(any('訓練用假人 中毒' in line for line in battle.log))

    def test_resolve_saves_the_complete_latest_round_log(self):
        battle = training_dummy_battle([player(1), player(2)], seed=1)
        target = battle.key(battle.living(1)[0])
        battle.submit(1, ACTION_ATTACK, target)
        battle.submit(2, ACTION_ATTACK, target)
        battle.resolve()
        first_round = list(battle.mechanics['last_round_log'])
        self.assertEqual(first_round[0], '── 第 1 回合 ──')
        self.assertTrue(any('玩家1 使用普通攻擊' in line for line in first_round))
        battle.submit(1, ACTION_ATTACK, target)
        battle.submit(2, ACTION_ATTACK, target)
        battle.resolve()
        second_round = battle.mechanics['last_round_log']
        self.assertEqual(second_round[0], '── 第 2 回合 ──')
        self.assertNotIn('── 第 1 回合 ──', second_round)


if __name__ == '__main__':
    unittest.main()
