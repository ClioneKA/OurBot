"""Checks for the measurement policy, not claims about human win rates."""
from collections import Counter
import unittest

from core.rpg_total_battle import ACTION_ATTACK
from core.rpg_witch_battle import witch_battle_from_participants
from scripts.simulate_witch_full_gear import party
from scripts.witch_mechanic_policy import choose_round


class WitchBalancePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.participants = party([50] * 4)

    def make(self):
        return witch_battle_from_participants(self.participants, ('anan', 'noah', 'meruru'), seed=44)

    def test_full_slots_and_only_unlocked_passives(self):
        for level in (10, 19, 20, 49, 50, 90, 120):
            for participant in party([level] * 4):
                state = participant['state']
                self.assertEqual(state['level'], level)
                self.assertEqual(len(state['equipped']), state['capacity'] + 2)
                self.assertEqual(participant['passive_id'] is not None, level >= 50)
                if level < 20:
                    self.assertTrue(all(r['skill_id'] <= 3 for r in participant['rules']))

    def test_policy_does_not_advance_battle_rng_or_combat_state(self):
        b = self.make()
        state = b.rng.getstate()
        hp = [f.hp for f in b.fighters]
        choose_round(b, Counter())
        self.assertEqual(b.rng.getstate(), state)
        self.assertEqual([f.hp for f in b.fighters], hp)
        self.assertEqual(b.round, 0)
        self.assertTrue(b.ready_to_resolve())

    def test_forced_brainwash_uses_only_legal_friendly_basics(self):
        b = self.make()
        b.dead_once.update(('noah', 'meruru'))
        b.prepare(b.witch('anan'))
        forced = [p for p in b.living(0) if b.forced(p, b.planning_round)]
        self.assertTrue(forced)
        choose_round(b, Counter())
        for actor in forced:
            choice = b.choices[actor.user_id]
            self.assertEqual(choice.action, ACTION_ATTACK)
            target = b.fighter_for_key(choice.target)
            self.assertEqual(target.team, 0)
            self.assertIsNot(target, actor)

    def test_fast_archer_interrupts_an_announced_spell(self):
        b = self.make()
        b.witch('meruru').hp //= 2
        b.prepare(b.witch('meruru'))
        counts = Counter()
        choose_round(b, counts)
        archer = next(p for p in b.living(0) if p.job == '弓兵')
        choice = b.choices[archer.user_id]
        self.assertEqual(b._skill(archer, choice.skill_slot)[1].effect, 'hindering_shot')
        self.assertEqual(choice.target, b.key(b.witch('meruru')))
        self.assertGreater(counts['interrupt'], 0)

    def test_breaks_painting_when_interrupts_are_on_cooldown(self):
        b = self.make()
        b.prepare(b.witch('noah'))
        for actor in b.living(0):
            for rule in actor.rules:
                if b._skill(actor, rule.slot)[1].effect in ('hindering_shot', 'shield_bash'):
                    actor.ready[rule.slot] = 100
        counts = Counter()
        choose_round(b, counts)
        self.assertGreater(counts['break_object'], 0)


if __name__ == '__main__':
    unittest.main()
