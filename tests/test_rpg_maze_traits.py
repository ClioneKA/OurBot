import unittest

from core.rpg_battle import Battle, Fighter, Rule, Skill, dump_battle, load_battle
from core.rpg_maze_traits import VALUES
from core.rpg_painted_maze_battle import apply_party_contracts


def encounter(contracts):
    stats = dict(HP=1000, 攻擊=100, 防禦=0, 治療量=100, 命中率=1000, 閃避率=0, 暴擊率=0)
    a = Fighter('玩家', 0, '裝甲步兵', stats, 50, [], user_id=1)
    e = Fighter('敵人', 1, '巨獸', dict(stats, HP=100000), 10, [], is_boss=True)
    battle = Battle([a, e], seed=1)
    apply_party_contracts(battle, contracts)
    battle.round = 1
    return battle, a, e


class MazeTraitTests(unittest.TestCase):
    def test_alternating_skills_gain_bonus_and_repetition_resets(self):
        b, a, e = encounter(['crimson:edge'])
        rule = Rule(1, 1, True, 'always', 'lowest')
        for effect in ('strike', 'break', 'strike'):
            b.use_skill(a, rule, Skill(effect, effect, 1, ''), e)
        self.assertEqual(a.passive_state['maze_edge_chain'], 0)
        self.assertTrue(any('第三段' in line for line in b.log))
        for _ in range(2):
            b.use_skill(a, rule, Skill('strike', 'strike', 1, ''), e)
        self.assertEqual(a.passive_state['maze_edge_chain'], 1)

    def test_four_hits_trigger_only_one_extra_hit_and_reload_preserves_counter(self):
        b, a, e = encounter(['crimson:precision'])
        for _ in range(3):
            b.basic_attack(a, e)
        b = load_battle(dump_battle(b))
        a, e = b.fighters
        b.basic_attack(a, e)
        self.assertEqual(a.combat_stats['hits'], 5)
        self.assertEqual(a.passive_state['maze_pursuit_hits'], 0)

    def test_support_skill_shield_does_not_stack_and_emergency_is_once(self):
        b, a, e = encounter(['azure:armor', 'azure:vitality'])
        for _ in range(3):
            b.use_skill(a, Rule(1, 1, True, 'always', 'self'), Skill('準備', 'stance', 1, ''), a)
        self.assertEqual(a.status_stacks['maze_trait_shield'], VALUES['armor'] * 10)
        b.apply_damage(a, 800, direct=True)
        self.assertEqual(a.status_stacks['maze_trait_shield'], VALUES['emergency'] * 10)
        b.apply_damage(a, 181, direct=True)
        self.assertEqual(a.status_stacks['maze_trait_shield'], 0)

    def test_basic_attack_reduces_one_cooldown_at_most_once_per_configured_interval(self):
        b, a, e = encounter(['gold:aim'])
        a.ready = {1: 20, 2: 19}
        b.basic_attack(a, e)
        self.assertEqual(a.ready, {1: 19, 2: 19})
        b.basic_attack(a, e)
        self.assertEqual(a.ready, {1: 19, 2: 19})
        b.round = 1 + VALUES['aim_interval']
        b.basic_attack(a, e)
        self.assertEqual(a.ready, {1: 18, 2: 19})

    def test_evasion_only_reduces_direct_hits_at_three_round_intervals(self):
        b, a, e = encounter(['gold:evasion'])
        b.apply_damage(a, 100)
        self.assertEqual(a.hp, 900)
        b.apply_damage(a, 100, direct=True)
        self.assertEqual(a.hp, 835)
        b.apply_damage(a, 100, direct=True)
        self.assertEqual(a.hp, 735)
        b.round = 4
        b.apply_damage(a, 100, direct=True)
        self.assertEqual(a.hp, 670)

    def test_effective_skill_healing_arms_one_drain_without_self_rearming(self):
        b, a, e = encounter(['verdant:renewal'])
        a.hp = 400
        b.use_skill(a, Rule(1, 1, True, 'always', 'self'), Skill('治療', 'heal', 1, ''), a)
        self.assertTrue(a.passive_state['maze_drain_ready'])
        before = a.hp
        b.basic_attack(a, e)
        self.assertGreater(a.hp, before)
        self.assertNotIn('maze_drain_ready', a.passive_state)
        before = a.hp
        b.basic_attack(a, e)
        self.assertEqual(a.hp, before)

    def test_two_debuffs_trigger_one_erosion_extra_hit_and_insight_respects_immunity(self):
        b, a, e = encounter(['violet:focus', 'violet:insight'])
        e.effects = dict(poison=9, immunity=9)
        b.basic_attack(a, e)
        self.assertNotIn('weak', e.effects)
        e.effects.pop('immunity')
        b.basic_attack(a, e)
        self.assertIn('weak', e.effects)
        self.assertEqual(a.combat_stats['hits'], 3)
        b.basic_attack(a, e)
        self.assertEqual(a.combat_stats['hits'], 4)

    def test_ruin_cost_cannot_kill_and_kill_refund_is_once_per_skill(self):
        b, a, e = encounter(['black:ruin'])
        a.hp = 500
        e.hp = 1
        b.use_skill(a, Rule(1, 1, True, 'always', 'lowest'), Skill('斬擊', 'strike', 1, ''), e)
        self.assertEqual(a.hp, 500 - VALUES['cost'] * 10 + VALUES['refund'] * 10)
        e.hp = 10000
        a.hp = 1
        b.use_skill(a, Rule(1, 1, True, 'always', 'lowest'), Skill('斬擊', 'strike', 1, ''), e)
        self.assertEqual(a.hp, 1)

    def test_gamble_saves_once_across_reload_and_never_stops_dot_damage(self):
        b, a, e = encounter(['black:gamble'])
        b.apply_damage(a, 2000, direct=True)
        self.assertEqual(a.hp, 1)
        b.heal(a, a, 500)
        self.assertEqual(a.hp, 1)
        b.round = 3
        b.heal(a, a, 100)
        self.assertEqual(a.hp, 101)
        b = load_battle(dump_battle(b))
        a = b.fighters[0]
        b.apply_damage(a, 200, direct=True)
        self.assertEqual(a.hp, 0)
        b, a, e = encounter(['black:gamble'])
        b.apply_damage(a, 2000)
        self.assertEqual(a.hp, 0)

    def test_round_step_triggers_shelter_without_reviving_fallen(self):
        from unittest.mock import patch
        b, a, e = encounter(['verdant:shelter'])
        a.hp = 500
        b.round = 2
        with patch.object(b, 'act'):
            b.step()
        self.assertEqual(a.hp, 540)

    def test_trait_combinations_replay_identically_after_snapshot_reload(self):
        combos = [
            ['crimson:edge', 'crimson:precision', 'gold:aim'],
            ['azure:armor', 'azure:vitality', 'gold:evasion'],
            ['verdant:renewal', 'verdant:shelter', 'violet:insight'],
            ['violet:focus', 'black:ruin', 'black:gamble'],
        ]
        for contracts in combos:
            with self.subTest(contracts=contracts):
                b, a, e = encounter(contracts)
                a.rules = [Rule(1, 1, True, 'always', 'lowest', 1)]
                a.hp = 400
                for _ in range(3):
                    b.step()
                resumed = load_battle(dump_battle(b))
                for _ in range(5):
                    b.step()
                    resumed.step()
                self.assertEqual(dump_battle(b), dump_battle(resumed))


if __name__ == '__main__':
    unittest.main()
