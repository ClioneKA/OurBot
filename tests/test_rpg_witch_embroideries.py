import unittest
from core.rpg_battle import Battle, Fighter, Skill, Rule
from core import rpg_witch_embroideries as effects


def player(uid, job='僧侶'):
    return Fighter(str(uid), 0, job, {'HP': 1000, '攻擊': 100, '防禦': 0,
        '治療量': 100, '命中率': 100, '閃避率': 0, '暴擊率': 0}, 10, [], user_id=uid)


class WitchEmbroideryTests(unittest.TestCase):
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
