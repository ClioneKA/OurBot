from dataclasses import asdict
import unittest

from core.rpg_battle import Battle, Fighter, SKILLS, default_rules, dump_battle, load_battle
from core.rpg_painted_maze import draw_painting_route
from core.rpg_painted_maze_battle import (
    FINAL_MAX_ROUNDS,
    PAINTING_MAX_ROUNDS,
    STAGE_PROFILE,
    apply_party_contracts,
    build_painting_battle,
    carry_party_state,
    build_final_battle,
    final_monster,
    painting_monster,
    simulate_painting,
    simulate_room_painting,
    simulate_final_battle,
)


def participant(user_id=1):
    return {
        'id': user_id,
        'name': f'玩家{user_id}',
        'state': {
            'level': 60,
            'job': '裝甲步兵',
            'combat': {
                'HP': 2_000, '攻擊': 500, '防禦': 220, '治療量': 100,
                '命中率': 140, '閃避率': 20, '暴擊率': 25, '速度': 55,
            },
            'speed': 55,
            'stability': (100, 100),
            'equipped': {'武器': 'starter:club'},
            'critical_damage_percent': 150,
        },
        'rules': [asdict(rule) for rule in default_rules('裝甲步兵')],
        'passive_id': None,
    }


class PaintedMazeBattleTests(unittest.TestCase):
    def test_painting_profile_uses_requested_content_level_and_party_scaling(self):
        painting = draw_painting_route(3)[0]
        solo = painting_monster(painting, 1)
        party = painting_monster(painting, 8)
        self.assertEqual((solo['content_level'], solo['tier'], solo['quality']), (50, 5, '普通'))
        self.assertAlmostEqual(
            party['profile']['hp'] * 8 / solo['profile']['hp'], 1 + .85 * 7)
        self.assertAlmostEqual(solo['profile']['attack'] / STAGE_PROFILE[50]['attack'], .55)
        self.assertAlmostEqual(party['profile']['attack'] / STAGE_PROFILE[50]['attack'], 1.15)
        self.assertEqual(solo['name'], painting['name'])

    def test_contracts_apply_party_benefits_and_violet_checks_target_debuff(self):
        player = Fighter('玩家', 0, '僧侶', {
            'HP': 1000, '攻擊': 100, '防禦': 100, '治療量': 200,
            '命中率': 100, '閃避率': 0, '暴擊率': 0, '速度': 50,
        }, 50, [])
        enemy = Fighter('畫作', 1, '巨獸', {
            'HP': 1000, '攻擊': 100, '防禦': 100, '治療量': 0,
            '命中率': 100, '閃避率': 0, '暴擊率': 0, '速度': 50,
        }, 50, [])
        battle = Battle([player, enemy], seed=1)
        apply_party_contracts(
            battle, ['crimson', 'azure', 'gold', 'gold', 'verdant', 'violet', 'black'])
        self.assertEqual(player.damage_dealt_percent, 12)
        self.assertEqual(battle.direct_damage_multiplier(player), 1.08)
        self.assertEqual(player.damage_taken_percent, -2)
        self.assertEqual(player.stats['暴擊率'], 2)
        self.assertEqual((player.speed, player.cooldown_reduction), (66, 1))
        self.assertEqual(player.stats['治療量'], 220)
        self.assertEqual(battle.debuff_damage_multiplier(player, enemy), 1.0)
        enemy.effects['break'] = 1
        battle.round = 1
        self.assertEqual(battle.debuff_damage_multiplier(player, enemy), 1.08)

    def test_carry_state_heals_survivors_revives_fallen_and_adds_stage_recovery(self):
        survivor = Fighter('存活', 0, '裝甲步兵', {
            'HP': 1000, '攻擊': 100, '防禦': 100, '治療量': 0,
            '命中率': 100, '閃避率': 0, '暴擊率': 0,
        }, 50, [], user_id=1)
        fallen = Fighter('倒下', 0, '裝甲步兵', dict(survivor.stats), 50, [], user_id=2)
        enemy = Fighter('畫作', 1, '巨獸', dict(survivor.stats), 50, [])
        survivor.hp, fallen.hp, enemy.hp = 400, 0, 0
        battle = Battle([survivor, fallen, enemy], seed=1)
        battle.result = '勝利'
        state = carry_party_state(battle, 3, ['verdant'])
        self.assertEqual(state['1']['hp'], 1000)
        self.assertEqual(state['2']['hp'], 600)
        self.assertFalse(state['2']['fallen'])

    def test_build_restores_carried_hp_and_simulation_stops_at_round_limit(self):
        painting = draw_painting_route(9)[0]
        battle = build_painting_battle(
            [participant()], painting, 44, ['crimson'], {'1': {'hp': 777}})
        player = next(fighter for fighter in battle.fighters if fighter.team == 0)
        self.assertEqual(player.hp, 777)
        self.assertEqual(battle.max_rounds, PAINTING_MAX_ROUNDS)
        self.assertIn(painting['name'], battle.log[0])

        result = simulate_painting([participant()], painting, 44, 1, ['crimson'], {'1': {'hp': 777}})
        restored = load_battle(result['battle'])
        self.assertEqual(restored.result, result['result'])
        self.assertLessEqual(result['rounds'], PAINTING_MAX_ROUNDS)
        self.assertIn(result['result'], ('勝利', '戰敗', '平手', '平手（達回合上限）'))

    def test_room_simulation_uses_saved_route_progress_contracts_and_hp(self):
        paintings = draw_painting_route(71)
        room = {
            'status': 'running', 'boss_index': 1, 'seed': 71,
            'paintings': paintings, 'participants': [participant()],
            'contracts': ['azure'], 'party_state': {'1': {'hp': 900}},
        }
        result = simulate_room_painting(room)
        battle = load_battle(result['battle'])
        self.assertEqual(battle.mechanics['painted_maze_contracts'], {'azure': 1})
        self.assertIn(paintings[1]['name'], battle.log[0])
        self.assertLessEqual(result['rounds'], PAINTING_MAX_ROUNDS)

    def test_final_routes_apply_contract_benefits_backlash_and_carried_hp(self):
        room = {
            'status': 'running', 'boss_index': 9, 'seed': 71, 'route': 'noah',
            'participants': [participant()], 'contracts': ['gold', 'gold', 'black'],
            'party_state': {'1': {'hp': 888}},
        }
        monster = final_monster(room)
        self.assertEqual(monster['name'], '繪畫魔女．城崎諾亞')
        battle = build_final_battle(room)
        player = next(f for f in battle.fighters if f.team == 0)
        boss = next(f for f in battle.fighters if f.team == 1 and f.is_boss)
        self.assertEqual((battle.max_rounds, player.hp), (FINAL_MAX_ROUNDS, 888))
        self.assertEqual(player.cooldown_reduction, 1)
        self.assertEqual(boss.damage_dealt_percent, 3)
        self.assertEqual(battle.mechanics['maze_final_contracts'], {'gold': 2, 'black': 1})

        shadow = dict(room, route='shadow')
        self.assertEqual(final_monster(shadow)['name'], '繪畫之影')
        result = simulate_final_battle(shadow)
        self.assertLessEqual(result['rounds'], FINAL_MAX_ROUNDS)
        self.assertIn(result['result'], ('勝利', '戰敗', '平手', '平手（達回合上限）'))

    def test_final_contract_backlash_shield_counter_corruption_heal_and_extra_action(self):
        stats = {'HP': 1000, '攻擊': 100, '防禦': 100, '治療量': 0,
                 '命中率': 100, '閃避率': 0, '暴擊率': 0, '速度': 50}
        player = Fighter('玩家', 0, '裝甲步兵', stats, 50, [], user_id=1)
        boss = Fighter('諾亞', 1, '城崎諾亞', stats, 50, [], is_boss=True)
        battle = Battle([player, boss], seed=1)
        battle.mechanics = {
            'maze_final': True,
            'maze_final_contracts': {
                'azure': 1, 'crimson': 3, 'violet': 1, 'verdant': 1, 'black': 3},
            'maze_final_shield': 0, 'maze_final_phases': [], 'maze_final_direct_hits': 0,
        }

        boss.hp = 690
        battle._maze_final_hit(player, boss, 710, 20)
        self.assertEqual(battle.mechanics['maze_final_shield'], 30)
        hp = boss.hp
        actual, _, _ = battle.apply_damage(boss, 30)
        self.assertEqual((actual, boss.hp, battle.mechanics['maze_final_shield']), (0, hp, 0))

        player.hp = 1000
        for _ in range(4):
            battle._maze_final_hit(player, boss, boss.hp + 1, 1)
        self.assertLess(player.hp, 1000)  # 緋紅三層累積五次直接命中後反擊
        battle._maze_final_hit(boss, player, player.hp + 1, 1)
        self.assertEqual(player.status_stacks['corruption'], 1)

        boss.hp = 500
        battle.round = 3
        calls = []
        battle.act = lambda actor: calls.append(actor.name)
        battle._maze_final_round_end()
        self.assertEqual(boss.hp, 510)
        self.assertEqual(calls, [])
        battle.round = 5
        battle._maze_final_round_end()
        self.assertEqual(calls, ['諾亞'])

    def test_source_crystal_stacks_reset_per_battle_and_are_serializable(self):
        stats = {'HP': 1000, '攻擊': 200, '防禦': 50, '治療量': 100,
                 '命中率': 200, '閃避率': 0, '暴擊率': 0, '速度': 50}
        infantry = Fighter('步兵', 0, '裝甲步兵', stats, 50, [], user_id=1)
        enemy = Fighter('畫作', 1, '巨獸', stats, 40, [])
        infantry.status_stacks['crystal_blooded_blade'] = 5
        battle = Battle([infantry, enemy], seed=1)
        battle.round = 1
        battle._direct_damage_taken(infantry, 10)
        context = battle._begin_passive_action(infantry, SKILLS['裝甲步兵'][0], enemy)
        self.assertAlmostEqual(context['multiplier'], 1.05)
        self.assertNotIn('crystal_blooded', infantry.passive_state)

        archer = Fighter('弓兵', 0, '弓兵', stats, 60, [], user_id=2)
        archer.status_stacks['crystal_endless_arrow'] = 3
        battle = Battle([archer, enemy], seed=2)
        context = battle._begin_passive_action(archer, target=enemy, basic=True)
        battle.hit(archer, enemy)
        battle._finish_passive_action(context)
        self.assertEqual(archer.passive_state['crystal_arrows'], 1)
        dump_battle(battle)  # Source stacks must survive persisted automatic battles.

        fresh = Fighter('新一戰', 0, '弓兵', stats, 60, [], user_id=2)
        self.assertEqual(fresh.passive_state, {})

    def test_final_phase_transition_add_and_shadow_skill_cycle(self):
        room = {
            'status': 'running', 'boss_index': 9, 'seed': 888, 'route': 'noah',
            'participants': [participant()], 'contracts': ['crimson', 'azure', 'violet'],
            'party_state': {'1': {'hp': 1500}},
        }
        battle = build_final_battle(room)
        player = next(f for f in battle.fighters if f.team == 0)
        boss = next(f for f in battle.fighters if f.is_boss)
        boss.hp = boss.stats['HP'] * 69 // 100
        battle._maze_final_hit(player, boss, boss.stats['HP'] * 71 // 100, 100)
        self.assertEqual(battle.mechanics['maze_final_phase'], 2)
        self.assertTrue(any(f.job == '未乾色塊' for f in battle.living(1)))
        boss.hp = boss.stats['HP'] * 34 // 100
        battle._maze_final_hit(player, boss, boss.stats['HP'] * 36 // 100, 100)
        self.assertEqual(battle.mechanics['maze_final_phase'], 3)
        self.assertFalse(any(f.job == '未乾色塊' for f in battle.living(1)))

        shadow = build_final_battle(dict(room, route='shadow'))
        shadow_boss = next(f for f in shadow.fighters if f.is_boss)
        shadow.shadow_act(shadow_boss)
        shadow.shadow_act(shadow_boss)
        self.assertEqual(shadow.mechanics['maze_shadow_skill_index'], 2)
        self.assertTrue(any('緋紅影擊' in line for line in shadow.log))
        self.assertGreater(shadow.mechanics['maze_final_shield'], 0)


if __name__ == '__main__':
    unittest.main()
