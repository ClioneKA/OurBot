import json
import unittest
from itertools import combinations

from core.rpg_battle import Rule
from core.rpg_total_battle import ACTION_ATTACK, ACTION_SKILL, TotalRaidError, dump_total_battle, load_total_battle
from core.rpg_witch_battle import witch_battle_from_participants, ACTION_DEFEND, STATE_FIELDS
from core.rpg_witch_catalog import IDS
from dataclasses import asdict


class WitchBattleTests(unittest.TestCase):
    def make(self, phase=0, ids=('anan', 'noah', 'meruru')):
        jobs = ('裝甲步兵', '裝甲步兵', '騎士', '弓兵', '弓兵', '僧侶')
        participants = [dict(id=i, name=f'玩家{i}', state=dict(
            job=job, level=50, stage=2, total=(100, 100, 100, 100, 100), speed=50,
            combat={'HP': 20000, '攻擊': 500, '防禦': 200, '治療量': 500,
                    '命中率': 100, '閃避率': 0, '暴擊率': 0},
            equipped={'武器': 'starter:club'}),
            rules=[asdict(Rule(slot, slot, True, 'always', 'lowest', skill_id=slot))
                   for slot in (1, 2, 3)]) for i, job in enumerate(jobs, 1)]
        b = witch_battle_from_participants(participants, ids, seed=44)
        b.dead_once.update(('noah', 'meruru')[:phase])
        return b

    def test_confirm_edit_timeout_and_takeover(self):
        b = self.make()
        uid = b.living(0)[0].user_id
        b.submit(uid, ACTION_DEFEND)
        self.assertIn(uid, b.waiting_player_ids())
        b.confirm(uid)
        self.assertNotIn(uid, b.waiting_player_ids())
        b.submit(uid, ACTION_DEFEND)
        self.assertIn(uid, b.waiting_player_ids())
        for _ in range(3):
            b.resolve(use_defaults=True)
        self.assertIn(uid, b.auto_players)
        b = load_total_battle(json.loads(json.dumps(dump_total_battle(b))))
        b.takeover(uid)
        self.assertNotIn(uid, b.auto_players)
        self.assertEqual(b.timeout_streak[uid], 0)
        self.assertIn(uid, b.waiting_player_ids())

    def test_brainwash_ui_and_strict_forced_validation(self):
        b = self.make(2)
        b.prepare(b.witch('anan'))
        p = b.fighter_for_key(next(iter(b.commands)))
        self.assertEqual(len(b.commands), 4)
        self.assertEqual(len(b.available_actions(p.user_id)), 1)
        targets = b.valid_targets(p.user_id, ACTION_ATTACK)
        self.assertNotIn(b.key(p), targets)
        self.assertTrue(all(b.fighter_for_key(k).team == 0 for k in targets))
        with self.assertRaises(TotalRaidError):
            b.submit(p.user_id, ACTION_DEFEND)
        with self.assertRaises(TotalRaidError):
            b.submit(p.user_id, ACTION_ATTACK, b.key(b.witch('anan')))
        b.submit(p.user_id, ACTION_ATTACK, targets[0])
        b.confirm(p.user_id)
        restored = load_total_battle(json.loads(json.dumps(dump_total_battle(b))))
        self.assertTrue(restored.forced(restored._player(p.user_id), 1))
        self.assertEqual(restored.confirmed, {p.user_id})

    def test_healing_ui_targets_witches(self):
        b = self.make()
        p = next(p for p in b.living(0) if p.job == '僧侶')
        p.effects['brainwash'] = 1
        b.commands[b.key(p)] = ('brainwash', 1)
        slot = next(r.slot for r in p.rules if b._skill(p, r.slot)[1].effect == 'heal')
        self.assertEqual(b.valid_targets(p.user_id, ACTION_SKILL, slot), [b.key(w) for w in b.witches()])

    def test_no_look_target_rejected(self):
        b = self.make()
        p = b.living(0)[0]
        paint = b.add_object(b.witch('noah'), 'animal_painting', .1)
        p.effects['no_look'] = 1
        self.assertNotIn(paint, b.valid_targets(p.user_id, ACTION_ATTACK))
        with self.assertRaises(TotalRaidError):
            b.submit(p.user_id, ACTION_ATTACK, paint)

    def test_all_286_trios_restart_mid_battle(self):
        for ids in combinations(IDS, 3):
            uninterrupted = self.make(ids=ids)
            restored = load_total_battle(json.loads(json.dumps(dump_total_battle(uninterrupted))))
            while not uninterrupted.result:
                uninterrupted.resolve(use_defaults=True)
                restored.resolve(use_defaults=True)
                left, right = dump_total_battle(restored), dump_total_battle(uninterrupted)
                left.pop('witch_state')
                right.pop('witch_state')
                self.assertEqual(left, right, ids)
                # Sets are semantically unordered, including across JSON restarts.
                for field in STATE_FIELDS:
                    self.assertEqual(getattr(restored, field), getattr(uninterrupted, field), (ids, field))
                restored = load_total_battle(json.loads(json.dumps(dump_total_battle(restored))))
