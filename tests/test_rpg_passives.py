from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_battle import (Battle, Fighter, Rule, Tactics, dump_battle, load_battle,
                             raid_battle, rule_skill)
from core.rpg_character import CharacterError


def fighter(job, passive_id=None, team=0, hp=1000, attack=100, healing=100, user_id=None):
    return Fighter(job, team, job,
                   {'HP': hp, '攻擊': attack, '防禦': 0, '治療量': healing,
                    '命中率': 200, '閃避率': 0, '暴擊率': 0},
                   50, [], stability=(100, 100), passive_id=passive_id, user_id=user_id)


def use(battle, actor, skill_id, target=None, slot=1):
    rule = Rule(slot, slot, True, 'always', 'lowest', skill_id)
    battle.use_skill(actor, rule, rule_skill(actor.job, rule), target or actor)


class PassiveStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.tactics = Tactics(self.store)

    def test_level_boundary_selection_and_job_isolation(self):
        self.store.award_voice([(1, 1, level_floor(50) - 1)])
        self.assertEqual(self.tactics.available_passives(1, 1, '裝甲步兵'), ())
        with self.assertRaises(CharacterError):
            self.tactics.equip_passive(1, 1, '裝甲步兵', 1)
        self.store.award_voice([(1, 1, 1)])
        self.assertEqual(len(self.tactics.available_passives(1, 1, '裝甲步兵')), 3)
        selected = self.tactics.equip_passive(1, 1, '裝甲步兵', 2)
        self.assertEqual((selected.name, self.tactics.passive(1, 1, '裝甲步兵').id),
                         ('攻守輪轉', 2))
        self.assertIsNone(self.tactics.passive(1, 1, '騎士'))
        self.assertEqual(self.tactics.available_passives(1, 1, '民兵'), ())

    def test_raid_and_battle_snapshot_keep_passive(self):
        player = fighter('弓兵', 1, user_id=7)
        participant = dict(id=7, name='玩家',
                           state=dict(level=50, job='弓兵', combat=player.stats,
                                      total=[1] * 5, equipped={'武器': 'test'}),
                           rules=[asdict(Rule(1, 1, True, 'always', 'lowest', 1))],
                           passive_id=1)
        battle = raid_battle([participant], {'kind': '巨獸', 'name': '巨獸'}, 1)
        self.assertEqual(battle.fighters[0].passive_id, 1)
        battle.fighters[0].passive_state['arrow_tempo'] = 4
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual((restored.fighters[0].passive_id,
                          restored.fighters[0].passive_state['arrow_tempo']), (1, 4))


class InfantryPassiveTests(unittest.TestCase):
    def test_tempered_combo_third_distinct_skill_bonuses_and_breaks(self):
        actor, enemy = fighter('裝甲步兵', 1), fighter('敵人', team=1, hp=5000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 1, enemy)
        use(battle, actor, 2, enemy)
        before = enemy.hp
        use(battle, actor, 5, enemy)
        self.assertEqual(before - enemy.hp, 330)
        self.assertTrue(enemy.has('break', 1))
        self.assertEqual(actor.passive_state['chain'], 0)

    def test_offense_defense_rotation_bonuses_and_heals(self):
        actor, enemy = fighter('裝甲步兵', 2), fighter('敵人', team=1, hp=5000)
        actor.hp = 500
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 3)
        before = enemy.hp
        use(battle, actor, 1, enemy)
        # Existing 攻守架勢 attack +20% stacks multiplicatively with one 15% guard layer.
        self.assertEqual(before - enemy.hp, 220)
        before_hp = actor.hp
        use(battle, actor, 3)
        self.assertEqual(actor.hp - before_hp, 30)

    def test_blood_rage_builds_once_per_round_and_consumes(self):
        actor, enemy = fighter('裝甲步兵', 3), fighter('敵人', team=1, hp=5000)
        actor.hp = 500
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        battle.hit(enemy, actor, precise=True)
        battle.hit(enemy, actor, precise=True)
        self.assertEqual(actor.passive_state['blood_rage'], 2)
        use(battle, actor, 3)
        self.assertEqual(actor.passive_state['blood_rage'], 3)
        before = enemy.hp
        use(battle, actor, 1, enemy)
        # Existing 攻守架勢 attack +20% also remains active.
        self.assertEqual(before - enemy.hp, 249)
        self.assertEqual(actor.passive_state['blood_rage'], 0)


class KnightPassiveTests(unittest.TestCase):
    def test_revenge_bonuses_next_active_attack(self):
        actor, enemy = fighter('騎士', 1), fighter('敵人', team=1, hp=5000)
        actor.hp = 500
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 1)
        battle.hit(enemy, actor, precise=True)
        self.assertEqual(actor.passive_state['revenge'], 1)
        before_hp, before_enemy = actor.hp, enemy.hp
        use(battle, actor, 4, enemy)
        self.assertEqual(before_enemy - enemy.hp, 134)
        self.assertEqual(actor.hp - before_hp, 20)

    def test_watch_empowers_guard_after_two_distinct_allies(self):
        actor = fighter('騎士', 2, user_id=1)
        first = fighter('弓兵', team=0, user_id=2)
        second = fighter('僧侶', team=0, user_id=3)
        enemy = fighter('敵人', team=1)
        battle = Battle([actor, first, second, enemy], 1)
        battle.round = 1
        use(battle, actor, 2)
        battle.hit(enemy, first, precise=True)
        battle.hit(enemy, second, precise=True)
        self.assertEqual(actor.passive_state['watch'], 2)
        battle.round = 2
        use(battle, actor, 2)
        self.assertEqual(actor.passive_state['watch'], 0)
        self.assertTrue(all(ally.has('watch_guard', 2) for ally in (actor, first, second)))

    def test_lance_shield_alternation(self):
        actor, enemy = fighter('騎士', 3), fighter('敵人', team=1, hp=5000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 4, enemy)
        self.assertEqual(actor.passive_state['lance_combo'], 'opening')
        before = enemy.hp
        use(battle, actor, 3, enemy)
        self.assertEqual(before - enemy.hp, 625)
        self.assertEqual(actor.passive_state['lance_combo'], 'momentum')
        before = enemy.hp
        use(battle, actor, 4, enemy)
        self.assertEqual(before - enemy.hp, 160)
        self.assertEqual(actor.passive_state['lance_combo'], 'opening')


class ArcherPassiveTests(unittest.TestCase):
    def test_arrow_tempo_scales_and_fires_followup(self):
        actor, enemy = fighter('弓兵', 1), fighter('敵人', team=1, hp=5000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        before = enemy.hp
        for _ in range(7):
            battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 842)
        self.assertEqual(actor.passive_state['arrow_tempo'], 0)

    def test_poison_ticks_build_toxicity_and_direct_hit_detonates(self):
        actor, enemy = fighter('弓兵', 2, user_id=7), fighter('敵人', team=1, hp=5000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        for slot in (1, 2, 3):
            use(battle, actor, 5, enemy, slot)
        battle.round = 2
        battle.tick_poison_arrows(enemy)
        key = '7'
        self.assertEqual(enemy.status_stacks['passive_toxicity'][key], 3)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 280)
        self.assertEqual(enemy.status_stacks['passive_toxicity'][key], 0)

    def test_three_crits_empower_next_damage_action(self):
        actor, enemy = fighter('弓兵', 3), fighter('敵人', team=1, hp=5000)
        actor.stats['暴擊率'] = 100
        actor.critical_damage_percent = 175
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        for _ in range(3):
            battle.basic_attack(actor, enemy)
        self.assertEqual(actor.passive_state['insight'], 3)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 244)
        self.assertEqual(actor.passive_state['insight'], 0)


class MonkPassiveTests(unittest.TestCase):
    def test_grace_echoes_every_fourth_effective_heal(self):
        actor = fighter('僧侶', 1)
        allies = [fighter('盟友', team=0, hp=1000) for _ in range(4)]
        enemy = fighter('敵人', team=1)
        battle = Battle([actor, *allies, enemy], 1)
        battle.round = 1
        for _ in range(3):
            allies[0].hp = 0
            use(battle, actor, 1, allies[0])
        self.assertEqual(actor.passive_state['grace'], 3)
        for ally in allies:
            ally.hp = 1
        use(battle, actor, 1, allies[0])
        self.assertEqual([ally.hp for ally in allies], [121, 16, 16, 16])
        self.assertEqual(actor.passive_state['grace'], 0)

    def test_threefold_hymn_collects_distinct_verses(self):
        actor, ally, enemy = fighter('僧侶', 2), fighter('盟友', team=0), fighter('敵人', team=1)
        ally.hp = 500
        battle = Battle([actor, ally, enemy], 1)
        battle.round = 1
        use(battle, actor, 1, ally)
        use(battle, actor, 2, ally)
        ally.effects['weak'] = 2
        before = ally.hp
        use(battle, actor, 3, ally)
        self.assertEqual(actor.passive_state['hymn_procs'], 1)
        self.assertEqual(ally.hp - before, 10)
        self.assertTrue(ally.has('hymn_strike', 1))

    def test_light_cycle_alternates_damage_and_healing(self):
        actor, ally, enemy = fighter('僧侶', 3), fighter('盟友', team=0), fighter('敵人', team=1, hp=5000)
        ally.hp = 0
        battle = Battle([actor, ally, enemy], 1)
        battle.round = 1
        battle.basic_attack(actor, enemy)
        self.assertEqual(actor.passive_state['radiance'], 1)
        use(battle, actor, 1, ally)
        self.assertEqual(ally.hp, 114)
        self.assertEqual(actor.passive_state['discipline'], 1)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 114)


if __name__ == '__main__':
    unittest.main()
