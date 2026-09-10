from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_blood_pays_hp_to_boost_only_current_skill_attack(self):
        actor, enemy = fighter('裝甲步兵', 3), fighter('敵人', team=1, hp=10000)
        enemy.stats['防禦'] = 100
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        before = enemy.hp
        use(battle, actor, 1, enemy)
        self.assertEqual(actor.hp, 950)
        self.assertEqual(before - enemy.hp, 205)  # (100 + 50) attack * 1.6 - 35 defense.
        self.assertEqual(actor.stats['攻擊'], 100)
        self.assertEqual(actor.combat_stats['damage_taken'], 50)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 65)
        self.assertEqual(actor.hp, 950)
        use(battle, actor, 3)  # Non-damaging preparation does not cost HP.
        self.assertEqual(actor.hp, 950)
        self.assertNotIn('blood_rage', actor.passive_state)

    def test_blood_cost_never_kills_and_requires_full_payment(self):
        for hp in (1, 49, 50, 51):
            with self.subTest(hp=hp):
                actor, enemy = fighter('裝甲步兵', 3), fighter('敵人', team=1, hp=10000)
                actor.hp = hp
                battle = Battle([actor, enemy], 1)
                before = enemy.hp
                use(battle, actor, 1, enemy)
                self.assertEqual(actor.hp, hp - 50 if hp > 50 else hp)
                self.assertEqual(before - enemy.hp, 240 if hp > 50 else 160)

    def test_blood_area_pays_once_and_missed_skill_still_pays(self):
        actor = fighter('裝甲步兵', 3)
        enemies = [fighter('敵人', team=1, hp=10000) for _ in range(3)]
        battle = Battle([actor, *enemies], 1)
        use(battle, actor, 4, enemies[0])
        self.assertEqual(actor.hp, 950)
        self.assertTrue(all(enemy.hp == 10000 - 180 for enemy in enemies))
        actor.stats['命中率'] = 0
        before = enemies[0].hp
        with patch.object(battle.rng, 'random', return_value=.99):
            use(battle, actor, 1, enemies[0])
        self.assertEqual(actor.hp, 900)
        self.assertEqual(enemies[0].hp, before)
        self.assertIsNone(battle._passive_action)

    def test_blood_bonus_equals_paid_hp_across_health_and_attack_values(self):
        for maximum, attack, cost in ((1980, 576, 99), (1999, 200, 99),
                                      (2000, 200, 100), (19, 100, 1)):
            with self.subTest(maximum=maximum, attack=attack):
                actor = fighter('裝甲步兵', 3, hp=maximum, attack=attack)
                enemy = fighter('敵人', team=1, hp=10000)
                battle = Battle([actor, enemy], 1)
                before = enemy.hp
                use(battle, actor, 2, enemy)  # Break uses 100% attack.
                self.assertEqual(actor.hp, maximum - cost)
                self.assertEqual(before - enemy.hp, attack + cost)
                self.assertEqual(actor.stats['攻擊'], attack)


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

    def test_lance_shield_alternation_stacks_and_caps(self):
        actor, enemy = fighter('騎士', 3), fighter('敵人', team=1, hp=10000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        for skill, expected, layers in ((4, 120, 0), (3, 500, 1), (4, 132, 2),
                                        (3, 600, 3), (4, 156, 4), (3, 700, 5),
                                        (4, 180, 5), (3, 750, 5)):
            before = enemy.hp
            use(battle, actor, skill, enemy)
            self.assertEqual(before - enemy.hp, expected)
            self.assertEqual(actor.passive_state.get('lance_stacks', 0), layers)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 100)
        self.assertEqual(actor.passive_state['lance_stacks'], 5)

    def test_lance_repeats_misses_and_snapshot_keep_progress(self):
        actor, enemy = fighter('騎士', 3), fighter('敵人', team=1, hp=10000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 4, enemy)
        use(battle, actor, 4, enemy)
        actor.stats['命中率'] = 0
        with patch.object(battle.rng, 'random', return_value=.99):
            use(battle, actor, 3, enemy)
        self.assertEqual(actor.passive_state.get('lance_stacks', 0), 0)
        self.assertEqual(actor.passive_state['lance_last'], 'shield_bash')
        actor.stats['命中率'] = 200
        battle.basic_attack(actor, enemy)
        use(battle, actor, 3, enemy)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        actor, enemy = restored.fighters
        self.assertEqual(actor.passive_state['lance_stacks'], 1)
        before = enemy.hp
        use(restored, actor, 4, enemy)
        self.assertEqual(before - enemy.hp, 132)
        self.assertEqual(actor.passive_state['lance_stacks'], 2)


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

    def test_poison_ticks_grow_without_detonating_or_resetting(self):
        actor, enemy = fighter('弓兵', 2, user_id=7), fighter('敵人', team=1, hp=10000)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        use(battle, actor, 5, enemy)
        for turn, expected in ((2, 70), (3, 91)):
            battle.round = turn
            before = enemy.hp
            battle.tick_poison_arrows(enemy)
            self.assertEqual(before - enemy.hp, expected)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['7'], 2)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 100)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['7'], 2)
        # A new poison arrow continues from the retained target-specific stacks.
        use(battle, actor, 5, enemy)
        for turn, expected in ((4, 112), (5, 133)):
            battle.round = turn
            before = enemy.hp
            battle.tick_poison_arrows(enemy)
            self.assertEqual(before - enemy.hp, expected)
        use(battle, actor, 5, enemy)
        for turn, expected in ((6, 154), (7, 175)):
            battle.round = turn
            before = enemy.hp
            battle.tick_poison_arrows(enemy)
            self.assertEqual(before - enemy.hp, expected)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['7'], 5)
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 100)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['7'], 5)

    def test_poison_stacks_are_source_and_target_specific_after_restore(self):
        first, second = fighter('弓兵', 2, user_id=7), fighter('弓兵', 2, user_id=8)
        enemy, other = fighter('敵人', team=1, hp=10000), fighter('敵人', team=1, hp=10000)
        battle = Battle([first, second, enemy, other], 1)
        battle.round = 1
        enemy.status_stacks['passive_toxicity'] = {'7': 5}
        use(battle, first, 5, enemy)
        use(battle, second, 5, enemy)
        use(battle, first, 5, other)
        battle = load_battle(json.loads(json.dumps(dump_battle(battle))))
        first, second, enemy, other = battle.fighters
        battle.round = 2
        before = enemy.hp
        battle.tick_poison_arrows(enemy)
        self.assertEqual(before - enemy.hp, 175 + 70)
        self.assertEqual(enemy.status_stacks['passive_toxicity'], {'7': 5, '8': 1})
        before = other.hp
        battle.tick_poison_arrows(other)
        self.assertEqual(before - other.hp, 70)
        self.assertEqual(other.status_stacks['passive_toxicity'], {'7': 1})

    def test_toxicity_boosts_normal_poison_from_same_source_only(self):
        first, second = fighter('弓兵', 2, user_id=7), fighter('弓兵', 2, user_id=8)
        enemy, other = fighter('敵人', team=1, hp=10000), fighter('敵人', team=1, hp=10000)
        battle = Battle([first, second, enemy, other], 1)
        for target in (enemy, other):
            target.status_stacks['passive_toxicity'] = {'7': 5}
        battle.apply_debuff(enemy, 'poison', 3, first)
        battle.apply_debuff(other, 'poison', 3, second)
        with patch.object(battle, 'act', return_value=None):
            battle.step()
        self.assertEqual(enemy.hp, 9500)  # Normal 2% HP poison * 2.5.
        self.assertEqual(other.hp, 9800)  # The other archer does not borrow stacks.
        self.assertEqual(first.combat_stats['damage_dealt'], 500)
        self.assertEqual(second.combat_stats['damage_dealt'], 200)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['7'], 5)

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
        use(battle, actor, 3, ally)
        self.assertNotIn('hymn_procs', actor.passive_state)
        self.assertEqual(set(actor.passive_state['hymn_verses']), {'mercy', 'courage'})
        before = ally.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(actor.passive_state['hymn_procs'], 1)
        self.assertEqual(ally.hp - before, 10)
        self.assertTrue(ally.has('hymn_strike', 1))
        before = enemy.hp
        battle.basic_attack(actor, enemy)
        self.assertEqual(before - enemy.hp, 110)
        self.assertNotIn('hymn_strike', actor.effects)
        self.assertEqual(actor.passive_state['hymn_verses'], ['offense'])

    def test_threefold_hymn_missed_attacks_still_grant_offense(self):
        for basic in (True, False):
            with self.subTest(basic=basic):
                actor = fighter('僧侶', 2)
                enemy = fighter('敵人', team=1)
                actor.stats['命中率'] = 0
                actor.hp = 500
                battle = Battle([actor, enemy], 1)
                battle.round = 1
                with patch.object(battle.rng, 'random', return_value=.99):
                    if basic:
                        battle.basic_attack(actor, enemy)
                    else:
                        use(battle, actor, 5, actor)
                self.assertEqual(enemy.hp, 1000)
                self.assertEqual(actor.combat_stats['misses'], 1)
                # Holy Light's effective healing still does not grant mercy.
                self.assertEqual(actor.passive_state['hymn_verses'], ['offense'])
                if not basic:
                    self.assertEqual(actor.hp, 570)

    def test_threefold_hymn_attack_verse_persists_without_duplicates(self):
        actor, enemy = fighter('僧侶', 2), fighter('敵人', team=1)
        battle = Battle([actor, enemy], 1)
        battle.round = 1
        battle.basic_attack(actor, enemy)
        battle = load_battle(json.loads(json.dumps(dump_battle(battle))))
        actor, enemy = battle.fighters
        battle.basic_attack(actor, enemy)
        use(battle, actor, 1, actor)  # Full HP is not effective healing.
        self.assertEqual(actor.passive_state['hymn_verses'], ['offense'])
        self.assertNotIn('hymn_procs', actor.passive_state)

    def test_threefold_hymn_drops_legacy_cleanse_verse(self):
        actor, enemy = fighter('僧侶', 2), fighter('敵人', team=1)
        actor.passive_state['hymn_verses'] = ['mercy', 'purity']
        snapshot = dump_battle(Battle([actor, enemy], 1))
        battle = load_battle(json.loads(json.dumps(snapshot)))
        actor, enemy = battle.fighters
        battle.round = 1
        use(battle, actor, 2, actor)
        self.assertEqual(set(actor.passive_state['hymn_verses']), {'mercy', 'courage'})
        self.assertNotIn('hymn_procs', actor.passive_state)
        battle.basic_attack(actor, enemy)
        self.assertEqual(actor.passive_state['hymn_procs'], 1)

    def test_threefold_hymn_keeps_three_proc_cap_and_buff_expiry(self):
        actor, enemy = fighter('僧侶', 2), fighter('敵人', team=1, hp=10000)
        battle = Battle([actor, enemy], 1)
        for turn in range(1, 5):
            battle.round = turn * 3  # Previous hymn bonus has expired.
            actor.hp = 500
            use(battle, actor, 1, actor)
            use(battle, actor, 2, actor)
            before = enemy.hp
            battle.basic_attack(actor, enemy)
            self.assertEqual(before - enemy.hp, 140)  # Blessing, not the new hymn.
            self.assertEqual(actor.passive_state['hymn_procs'], min(turn, 3))
            if turn <= 3:
                self.assertEqual(actor.hp, 610)
                self.assertEqual(actor.effects['hymn_strike'], battle.round + 1)
                self.assertFalse(actor.has('hymn_strike', battle.round + 2))
            else:
                self.assertEqual(actor.hp, 600)

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
