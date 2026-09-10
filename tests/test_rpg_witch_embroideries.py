import unittest
from unittest.mock import Mock
from core.rpg_battle import Battle, Fighter, Skill, Rule, dump_battle, load_battle
from core import rpg_witch_embroideries as effects


def player(uid, job='僧侶'):
    return Fighter(str(uid), 0, job, {'HP': 1000, '攻擊': 100, '防禦': 0,
        '治療量': 100, '命中率': 100, '閃避率': 0, '暴擊率': 0}, 10, [], user_id=uid)


class WitchEmbroideryTests(unittest.TestCase):
    def duel(self, embroidery, defensive=False):
        a, target = player(1), player(2, '騎士')
        target.team = 1
        (target if defensive else a).status_stacks['embroidery_' + embroidery] = 1
        return Battle([a, target], seed=1), a, target

    def damage(self, battle, actor, target, **kwargs):
        before = target.hp
        battle.hit(actor, target, precise=True, counterable=False, **kwargs)
        return before - target.hp

    def test_flower_strict_threshold_and_canvas_requires_explicit_object_tag(self):
        b, a, target = self.duel('witch_flower')
        target.hp = 300
        self.assertEqual(self.damage(b, a, target), 100)
        target.hp = 299
        self.assertEqual(self.damage(b, a, target), 105)
        b, a, target = self.duel('witch_canvas')
        target.name = '動物召喚畫'
        self.assertEqual(self.damage(b, a, target), 100)
        target.status_stacks['mechanism_object'] = 1
        self.assertEqual(self.damage(b, a, target), 106)

    def test_wish_requires_successful_enemy_debuff_and_consumes_once(self):
        b, a, target = self.duel('witch_wish')
        target.effects['immunity'] = 1
        self.assertFalse(b.apply_debuff(target, 'poison', 1, a))
        self.assertEqual(self.damage(b, a, target), 100)
        target.effects.clear()
        b.apply_debuff(target, 'poison', 1, a)
        b.apply_debuff(target, 'poison', 2, a)
        self.assertEqual(self.damage(b, a, target), 104)
        self.assertEqual(self.damage(b, a, target), 100)

    def test_embers_does_not_double_stack_and_fist_requires_long_single_skill(self):
        b, a, target = self.duel('witch_embers')
        target.effects.update(burn=1, poison=1)
        self.assertEqual(self.damage(b, a, target), 104)
        b, a, target = self.duel('witch_fist')
        for effect, cooldown, scope, expected in [('strike', 4, 'single', 104),
                                                  ('strike', 3, 'single', 100), ('area', 4, 'group', 100)]:
            context = b._begin_passive_action(a, Skill('測試', effect, cooldown, ''), target)
            self.assertEqual(self.damage(b, a, target, attack_scope=scope), expected)
            b._finish_passive_action(context)

    def test_star_and_feather_only_reduce_matching_direct_damage(self):
        b, a, target = self.duel('witch_star', defensive=True)
        self.assertEqual(self.damage(b, a, target), 100)
        target.effects['taunt'] = 1
        self.assertEqual(self.damage(b, a, target), 96)
        b, a, target = self.duel('witch_feather', defensive=True)
        self.assertEqual(self.damage(b, a, target), 100)
        self.assertEqual(self.damage(b, a, target, attack_scope='group'), 96)
        hp = target.hp
        b.apply_damage(target, 100)
        self.assertEqual(hp - target.hp, 100)

    def test_afterimage_tracks_enemy_and_consecutive_rounds_across_save(self):
        b, a, target = self.duel('witch_afterimage', defensive=True)
        b.round = 1
        self.assertEqual(self.damage(b, a, target), 100)
        b = load_battle(dump_battle(b))
        a, target = b.fighters
        b.round = 2
        self.assertEqual(self.damage(b, a, target), 96)
        self.assertEqual(self.damage(b, a, target), 96)
        b.round = 4
        self.assertEqual(self.damage(b, a, target), 100)

    def test_camera_bonuses_are_per_action_and_reset_on_non_attack(self):
        b, a, target = self.duel('witch_camera')
        a.stats['命中率'] = 90
        b.rng.random = Mock(return_value=.915)
        context = b._begin_passive_action(a, target=target, basic=True)
        self.assertFalse(b.hit(a, target, counterable=False))
        b._finish_passive_action(context)
        context = b._begin_passive_action(a, target=target, basic=True)
        self.assertTrue(b.hit(a, target, counterable=False))
        b._finish_passive_action(context)
        context = b._begin_passive_action(a, Skill('祝福', 'bless', 1, ''), a)
        b._finish_passive_action(context)
        context = b._begin_passive_action(a, target=target, basic=True)
        self.assertFalse(b.hit(a, target, counterable=False))
        b._finish_passive_action(context)

    def test_dawn_threshold_once_and_multihit(self):
        for hp, expected in ((500, 0), (501, 1)):
            p = player(1)
            p.hp = hp
            p.status_stacks['embroidery_witch_dawn'] = 1
            b = Battle([p])
            b.apply_damage(p, 9999, direct=True)
            self.assertEqual(p.hp, expected)
            if p.hp:
                b.apply_damage(p, 9999, direct=True)
                self.assertEqual(p.hp, 0)

    def test_exchange_one_rescuer_per_hit_and_once_per_battle(self):
        p, a, c = player(1), player(2), player(3)
        for f in (a, c):
            f.status_stacks['embroidery_witch_exchange'] = 1
        b = Battle([p, a, c])
        b.apply_damage(p, 1000, direct=True)
        self.assertEqual((p.hp, a.hp, c.hp), (1, 900, 1000))
        self.assertFalse(c.status_stacks.get('witch_exchange_used'))
        b.apply_damage(p, 1000, direct=True)
        self.assertEqual((p.hp, a.hp, c.hp), (1, 900, 900))
        b.apply_damage(p, 1000, direct=True)
        self.assertEqual(p.hp, 0)

    def test_exchange_requires_above_half_and_other_ally(self):
        p, a = player(1), player(2)
        for f in (p, a):
            f.status_stacks['embroidery_witch_exchange'] = 1
            f.hp = 500
        Battle([p, a]).apply_damage(p, 9999)
        self.assertEqual(p.hp, 0)
        self.assertEqual(a.hp, 500)

    def test_wings_threshold_before_heal_and_not_passive_heal(self):
        a, p = player(1), player(2)
        a.status_stacks['embroidery_witch_wings'] = 1
        for hp, expected in ((499, 105), (500, 100)):
            b = Battle([a, p])
            p.hp = hp
            context = b._begin_passive_action(a, Skill('補血', 'heal', 1, ''), p)
            self.assertEqual(b.heal(a, p, 100), expected)
            b._finish_passive_action(context)
        p.hp = 100
        self.assertEqual(b.heal(a, p, 100), 100)

    def test_echo_previous_teammate_and_expires_after_action(self):
        a, p = player(1), player(2)
        p.status_stacks['embroidery_witch_echo'] = 1
        b = Battle([a, p])
        skill = Skill('打擊', 'strike', 1, '')
        context = b._begin_passive_action(a, skill, p)
        b._finish_passive_action(context)
        context = b._begin_passive_action(p, skill, a)
        self.assertAlmostEqual(context['multiplier'], 1.04)
        b._finish_passive_action(context)
        context = b._begin_passive_action(p, skill, a)
        self.assertEqual(context['multiplier'], 1)
        b._finish_passive_action(context)
        context = b._begin_passive_action(a, Skill('治療', 'heal', 1, ''), p)
        b._finish_passive_action(context)
        context = b._begin_passive_action(p, Skill('治療', 'heal', 1, ''), a)
        self.assertAlmostEqual(context['healing_multiplier'], 1.04)
        self.assertEqual(context['multiplier'], 1)
