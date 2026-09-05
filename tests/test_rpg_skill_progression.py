from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.rpg import RPGStore, level_floor
from core.rpg_battle import (Battle, Fighter, Rule, Tactics, default_rules,
                             dump_battle, load_battle, raid_battle, rule_skill, unlocked_skills)
from core.rpg_character import CharacterError


def fighter(job='裝甲步兵', skill_id=4, team=0, hp=1000):
    return Fighter(job, team, job, {'HP': hp, '攻擊': 100, '防禦': 0, '治療量': 100,
                                  '命中率': 99, '閃避率': 0, '暴擊率': 0}, 10,
                   [Rule(1, 1, True, 'always', 'lowest', skill_id)] if team == 0 else [])


class ProgressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.tactics = Tactics(self.store)

    def test_level_boundary_all_jobs_and_militia(self):
        self.store.award_voice([(1, 1, level_floor(20) - 1)])
        for job in ('裝甲步兵', '騎士', '弓兵', '僧侶'):
            self.assertEqual(len(self.tactics.available(1, 1, job)), 3)
            with self.assertRaises(CharacterError):
                self.tactics.equip(1, 1, job, 1, 4)
        self.store.award_voice([(1, 1, 1)])
        for job in ('裝甲步兵', '騎士', '弓兵', '僧侶'):
            self.assertEqual(len(self.tactics.available(1, 1, job)), 5)
            self.tactics.equip(1, 1, job, 1, 4)
            self.tactics.equip(1, 1, job, 2, 5)
            self.assertEqual(len(self.tactics.rules(1, 1, job)), 3)
        self.assertEqual(len(unlocked_skills('民兵', 120)), 3)
        with self.assertRaises(CharacterError):
            self.tactics.equip(1, 1, '民兵', 1, 4)

    def test_replacement_persistence_priority_and_isolation(self):
        self.store.award_voice([(1, 1, level_floor(20))])
        self.tactics.configure(1, 1, '僧侶', 3, 1, False, 'ally_debuff', 'debuffed')
        self.tactics.equip(1, 1, '僧侶', 3, 5)
        rules = Tactics(self.store).rules(1, 1, '僧侶')
        self.assertEqual(rules[0], Rule(3, 1, False, 'ally50', 'lowest', 5, 50))
        self.tactics.configure(1, 1, '僧侶', 3, 2, True, 'always', 'self')
        rules = self.tactics.rules(1, 1, '僧侶')
        self.assertEqual(rules[1], Rule(3, 2, True, 'always', 'self', 5))
        self.assertEqual([r.priority for r in rules], [1, 2, 3])
        for guild, user, job in ((2, 1, '僧侶'), (1, 2, '僧侶'), (1, 1, '弓兵')):
            self.assertEqual(self.tactics.rules(guild, user, job), default_rules(job))
        with self.assertRaises(CharacterError):
            self.tactics.equip(1, 1, '僧侶', 2, 5)
        self.assertEqual(self.tactics.rules(1, 1, '僧侶'), rules)
        self.tactics.equip(1, 1, '僧侶', 3, 3)
        self.assertEqual(rule_skill('僧侶', self.tactics.rules(1, 1, '僧侶')[1]).name, '淨化')

    def test_re_equipping_bless_restores_strongest_target_default(self):
        self.store.award_voice([(1, 1, level_floor(20))])
        self.tactics.equip(1, 1, '僧侶', 2, 4)
        self.tactics.equip(1, 1, '僧侶', 2, 2)
        rule = next(rule for rule in self.tactics.rules(1, 1, '僧侶') if rule.slot == 2)
        self.assertEqual((rule.condition, rule.target), ('always', 'strongest'))

    def test_target_validation_follows_equipped_skill(self):
        self.store.award_voice([(1, 1, level_floor(20))])
        self.tactics.equip(1, 1, '騎士', 3, 4)
        with self.assertRaises(CharacterError):
            self.tactics.configure(1, 1, '騎士', 3, 1, True, 'always', 'self')
        self.tactics.equip(1, 1, '僧侶', 3, 4)
        with self.assertRaises(CharacterError):
            self.tactics.configure(1, 1, '僧侶', 3, 1, True, 'ally_debuff', 'debuffed')

    def test_legacy_table_migrates_without_changing_tactics(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RPGStore(Path(directory) / 'old.db')
            try:
                with store.db:
                    store.db.execute('CREATE TABLE rpg_tactics (guild_id INTEGER, user_id INTEGER, job TEXT, '
                                     'slot INTEGER, priority INTEGER, enabled INTEGER, condition TEXT, target TEXT, '
                                     'PRIMARY KEY (guild_id, user_id, job, slot))')
                    store.db.execute("INSERT INTO rpg_tactics VALUES (1, 1, '弓兵', 1, 1, 0, 'self40', 'strongest')")
                tactics = Tactics(store)
                self.assertEqual(tactics.rules(1, 1, '弓兵')[0], Rule(1, 1, False, 'self40', 'strongest', None, 40))
                self.assertEqual(len(Tactics(store).rules(1, 1, '弓兵')), 3)
            finally:
                store.close()


class AdvancedBattleTests(unittest.TestCase):
    def test_offensive_skills_use_expected_targets_and_power(self):
        for job, skill_id, expected in (('裝甲步兵', 4, [1.2, 1.2]), ('裝甲步兵', 5, [2.2]),
                                        ('騎士', 4, [1.2]), ('弓兵', 4, [0.75] * 3), ('弓兵', 5, [1.2])):
            with self.subTest(job=job, skill_id=skill_id):
                actor, a, b = fighter(job, skill_id), fighter(team=1), fighter(team=1)
                battle = Battle([actor, a, b], seed=1)
                battle.round = 1
                with patch.object(battle, 'hit', return_value=True) as hit:
                    battle.act(actor)
                self.assertEqual([c.args[2] for c in hit.call_args_list], expected)
                if skill_id == 4 and job == '裝甲步兵':
                    self.assertIs(hit.call_args_list[1].args[1], b)
                self.assertGreater(actor.ready[1], 1)
                self.assertIsNone(battle.select(actor))
                if job == '騎士':
                    self.assertEqual(a.effects['stun'], 2)
                if job == '弓兵' and skill_id == 5:
                    self.assertEqual(a.effects['poison'], 3)

    def test_misses_and_unarmed_attacks_do_not_apply_status(self):
        for job, skill_id in (('騎士', 4), ('弓兵', 5)):
            actor, enemy = fighter(job, skill_id), fighter(team=1)
            battle = Battle([actor, enemy], seed=1)
            with patch.object(battle, 'hit', return_value=False):
                battle.act(actor)
            self.assertEqual(enemy.effects, {})
            actor.ready.clear()
            actor.armed = False
            battle.act(actor)
            self.assertEqual(enemy.hp, 1000)
            self.assertEqual(enemy.effects, {})

    def test_triple_stops_after_target_dies(self):
        actor, enemy = fighter('弓兵', 4), fighter(team=1, hp=1)
        battle = Battle([actor, enemy], seed=1)
        battle.act(actor)
        self.assertEqual(enemy.hp, 0)
        self.assertEqual(len([line for line in battle.log if '傷害' in line]), 1)

    def test_healing_amounts_fixed_targets_and_no_resurrection(self):
        actor, ally, dead, enemy = fighter('僧侶', 4), fighter(), fighter(), fighter(team=1)
        actor.hp, ally.hp, dead.hp = 950, 100, 0
        battle = Battle([actor, ally, dead, enemy], seed=1)
        battle.act(actor)
        self.assertEqual((actor.hp, ally.hp, dead.hp, enemy.hp), (1000, 165, 0, 1000))
        actor.ready.clear()
        ally.hp = 1000
        self.assertIsNone(battle.select(actor))
        actor.rules = [Rule(1, 1, True, 'always', 'lowest', 5)]
        ally.hp = 100
        battle.act(actor)
        self.assertEqual(ally.hp, 280)
        knight = fighter('騎士', 5)
        knight.hp = 300
        battle.fighters.append(knight)
        battle.act(knight)
        self.assertEqual(knight.hp, 550)
        self.assertEqual(ally.hp, 280)
        knight.ready.clear()
        knight.hp = 1000
        self.assertIsNone(battle.select(knight))

    def test_equipped_skill_and_cooldown_survive_snapshot_reload(self):
        actor, enemy = fighter('弓兵', 5), fighter(team=1)
        battle = Battle([actor, enemy], seed=1)
        battle.round = 1
        battle.act(actor)
        saved = json.loads(json.dumps(dump_battle(battle)))
        restored = load_battle(saved)
        self.assertEqual(dump_battle(restored), dump_battle(battle))
        self.assertEqual(rule_skill('弓兵', restored.fighters[0].rules[0]).name, '毒箭')
        battle.step()
        restored.step()
        self.assertEqual(dump_battle(restored), dump_battle(battle))
        saved['fighters'][0]['rules'][0].pop('skill_id')
        legacy = load_battle(saved)
        self.assertEqual(rule_skill('弓兵', legacy.fighters[0].rules[0]).name, '連射')

    def test_raid_uses_equipped_skill_from_participant_snapshot(self):
        actor = fighter('騎士', 4)
        participant = dict(name='玩家', state=dict(job='騎士', combat=actor.stats, total=[1, 1, 1, 10, 1],
                                                  level=20, equipped={'武器': 'test'}),
                           rules=[asdict(r) for r in actor.rules])
        battle = raid_battle([participant], {'kind': '巨獸', 'name': '巨獸'}, seed=1)
        participant['rules'][0]['skill_id'] = 5
        self.assertEqual(rule_skill('騎士', battle.fighters[0].rules[0]).name, '盾擊')


if __name__ == '__main__':
    unittest.main()
