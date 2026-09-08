from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_painted_maze import (
    COLOR_CONTRACTS,
    CONTRACT_VOTE_SECONDS,
    MAX_PARTICIPANTS,
    ROOM_LIFETIME_SECONDS,
    PaintedMazeError,
    PaintedMazeStore,
    draw_painting_route,
)
from core.settings import RPGSettings


class PaintedMazeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rpg = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.addCleanup(self.rpg.close)
        self.characters = Characters(self.rpg, RPGSettings())
        self.repo = PaintedMazeStore(self.rpg)
        for user_id in range(1, 12):
            self.characters.create(1, user_id)

    @staticmethod
    def participant(user_id, level=50):
        return {'id': user_id, 'state': {'level': level, 'combat': {'HP': 1000}},
                'rules': [], 'passive_id': None}

    def test_create_requires_level_and_owned_painting_without_consuming_it(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        with self.assertRaisesRegex(PaintedMazeError, 'Lv.50'):
            self.repo.create(1, 1, 'painting:balloon', 49, now=100)
        room = self.repo.create(1, 1, 'painting:balloon', 50, channel_id=20, now=100, seed=123)
        self.assertEqual((room['route'], room['members'], room['status']), ('shadow', [1], 'lobby'))
        self.assertEqual(room['expires_at'], 100 + ROOM_LIFETIME_SECONDS)
        self.assertEqual(room['paintings'], draw_painting_route(123))
        self.assertEqual([len(room['paintings'][index:index + 3]) for index in (0, 3, 6)], [3, 3, 3])
        self.assertEqual([painting['tier'] for painting in room['paintings']], [50] * 3 + [55] * 3 + [60] * 3)
        self.assertEqual(self.characters.inventory_counts(1, 1)['painting:balloon'], 1)
        with self.assertRaisesRegex(PaintedMazeError, '另一個'):
            self.repo.create(1, 1, 'painting:balloon', 50, now=101)

    def test_admin_test_room_does_not_require_or_consume_entry(self):
        room = self.repo.create(
            1, 1, 'noah:unfinished', 50, now=100, require_entry=False)
        self.assertFalse(room['requires_entry'])
        started = self.repo.begin(room['id'], 1, [self.participant(1)], now=101)
        self.assertEqual((started['status'], started['entry_consumed']), ('running', False))
        self.assertEqual(self.characters.inventory_counts(1, 1).get('noah:unfinished', 0), 0)

    def test_lobby_is_one_to_eight_and_members_cannot_join_two_rooms(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        first = self.repo.create(1, 1, 'painting:balloon', 50, now=100)
        with self.assertRaisesRegex(PaintedMazeError, 'Lv.50'):
            self.repo.change_member(first['id'], 2, 49, now=101)
        for user_id in range(2, MAX_PARTICIPANTS + 1):
            self.repo.change_member(first['id'], user_id, 50, now=101)
        with self.assertRaisesRegex(PaintedMazeError, '隊伍已滿'):
            self.repo.change_member(first['id'], 9, 50, now=101)
        with self.assertRaisesRegex(PaintedMazeError, '房主不能退出'):
            self.repo.change_member(first['id'], 1, 50, leave=True, now=101)

        self.characters.grant_item(1, 10, 'noah:unfinished')
        second = self.repo.create(1, 10, 'noah:unfinished', 50, now=101)
        with self.assertRaisesRegex(PaintedMazeError, '另一個'):
            self.repo.change_member(second['id'], 2, 50, now=101)
        updated = self.repo.change_member(first['id'], 2, 50, leave=True, now=101)
        self.assertNotIn(2, updated['members'])
        self.assertIn(2, self.repo.change_member(second['id'], 2, 50, now=101)['members'])

    def test_begin_atomically_consumes_entry_locks_snapshots_and_resets_expiry(self):
        self.characters.grant_item(1, 1, 'noah:unfinished')
        room = self.repo.create(1, 1, 'noah:unfinished', 50, now=100)
        self.repo.change_member(room['id'], 2, 50, now=101)
        with self.assertRaisesRegex(PaintedMazeError, '只有房主'):
            self.repo.begin(room['id'], 2, [self.participant(1), self.participant(2)], now=200)
        with self.assertRaisesRegex(PaintedMazeError, '快照不完整'):
            self.repo.begin(room['id'], 1, [self.participant(1)], now=200)
        self.assertEqual(self.characters.inventory_counts(1, 1)['noah:unfinished'], 1)

        started = self.repo.begin(
            room['id'], 1, [self.participant(1), self.participant(2)], now=200)
        self.assertEqual((started['status'], started['entry_consumed']), ('running', True))
        self.assertEqual(started['expires_at'], 200 + ROOM_LIFETIME_SECONDS)
        self.assertEqual(self.characters.inventory_counts(1, 1).get('noah:unfinished', 0), 0)
        with self.assertRaisesRegex(PaintedMazeError, '已經開始'):
            self.repo.begin(room['id'], 1, started['participants'], now=201)

    def test_missing_painting_rolls_back_start_and_keeps_lobby(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.repo.create(1, 1, 'painting:balloon', 50, now=100)
        self.characters.consume_item(1, 1, 'painting:balloon')
        with self.assertRaisesRegex(PaintedMazeError, '已沒有'):
            self.repo.begin(room['id'], 1, [self.participant(1)], now=200)
        saved = self.repo.get(room['id'])
        self.assertEqual((saved['status'], saved['entry_consumed']), ('lobby', False))

    def test_cancel_admin_end_and_expiry_keep_consumption_rules(self):
        self.characters.grant_item(1, 1, 'painting:balloon', 2)
        lobby = self.repo.create(1, 1, 'painting:balloon', 50, now=100)
        closed = self.repo.close(lobby['id'], 1, now=110)
        self.assertEqual((closed['status'], closed['entry_consumed']), ('cancelled', False))
        self.assertEqual(self.characters.inventory_counts(1, 1)['painting:balloon'], 2)

        running = self.repo.create(1, 1, 'painting:balloon', 50, now=200)
        running = self.repo.begin(running['id'], 1, [self.participant(1)], now=201)
        with self.assertRaisesRegex(PaintedMazeError, '只能由管理員'):
            self.repo.close(running['id'], 1, now=202)
        ended = self.repo.close(running['id'], 99, administrator=True, now=203)
        self.assertEqual((ended['status'], ended['ended_by']), ('admin_ended', 99))
        self.assertEqual(self.characters.inventory_counts(1, 1)['painting:balloon'], 1)

        expiring = self.repo.create(1, 1, 'painting:balloon', 50, now=300)
        self.assertEqual(self.repo.expire_due(now=300 + ROOM_LIFETIME_SECONDS - 1), [])
        expired = self.repo.expire_due(now=300 + ROOM_LIFETIME_SECONDS)
        self.assertEqual([room['id'] for room in expired], [expiring['id']])
        self.assertEqual(self.repo.get(expiring['id'])['status'], 'expired')
        self.assertEqual(self.characters.inventory_counts(1, 1)['painting:balloon'], 1)

    def test_discord_ids_are_persistent_and_room_can_be_found_by_private_thread(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.repo.create(1, 1, 'painting:balloon', 50, now=100)
        attached = self.repo.attach_discord(
            room['id'], channel_id=20, thread_id=21, index_message_id=22, message_id=23)
        self.assertEqual(attached['thread_id'], 21)
        self.assertEqual(self.repo.by_thread(21)['id'], room['id'])

    def test_every_third_painting_opens_a_persistent_sixty_second_contract_vote(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.repo.create(1, 1, 'painting:balloon', 50, now=100, seed=123)
        self.repo.change_member(room['id'], 2, 50, now=101)
        room = self.repo.begin(
            room['id'], 1, [self.participant(1), self.participant(2)], now=110)
        room = self.repo.record_boss_victory(room['id'], 2, now=120)
        self.assertEqual((room['boss_index'], room['status']), (1, 'running'))
        self.repo.record_boss_victory(room['id'], 1, now=130)
        room = self.repo.record_boss_victory(
            room['id'], 2, sealed_rewards=[{'kind': 'outline', 'sealed': True}], now=140)
        vote = room['contract_vote']
        self.assertEqual((room['boss_index'], room['stage'], room['status']), (3, 1, 'contract'))
        self.assertEqual(vote['deadline'], 140 + CONTRACT_VOTE_SECONDS)
        self.assertEqual(len(vote['candidates']), 3)
        self.assertTrue(set(vote['candidates']) <= set(COLOR_CONTRACTS))
        self.assertEqual(room['sealed_rewards'], [{'kind': 'outline', 'sealed': True}])

        first, second, _third = vote['candidates']
        self.repo.cast_contract_vote(room['id'], 1, first, now=150)
        changed = self.repo.cast_contract_vote(room['id'], 1, second, now=160)
        self.assertEqual(changed['contract_vote']['votes'], {'1': second})
        self.repo.cast_contract_vote(room['id'], 2, second, now=170)
        with self.assertRaisesRegex(PaintedMazeError, '尚未截止'):
            self.repo.resolve_contract(room['id'], now=199)
        resolved = self.repo.resolve_contract(room['id'], now=200)
        self.assertEqual((resolved['status'], resolved['contracts']), ('running', [second]))
        self.assertIsNone(resolved['contract_vote'])
        self.assertEqual(resolved['last_contract_vote']['result'], second)

    def test_no_vote_contract_result_is_deterministic_and_restart_safe(self):
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.repo.create(1, 1, 'painting:balloon', 50, now=100, seed=888)
        room = self.repo.begin(room['id'], 1, [self.participant(1)], now=110)
        for timestamp in (120, 130, 140):
            room = self.repo.record_boss_victory(room['id'], 1, now=timestamp)
        expected_candidates = list(room['contract_vote']['candidates'])
        self.assertEqual(self.repo.resolve_contracts_due(now=199), [])
        resolved = self.repo.resolve_contracts_due(now=200)[0]
        self.assertIn(resolved['contracts'][0], expected_candidates)
        self.assertEqual(self.repo.resolve_contracts_due(now=201), [])

    def test_painting_settlement_is_idempotent_and_failure_ends_the_run(self):
        self.characters.grant_item(1, 1, 'painting:balloon', 2)
        room = self.repo.create(1, 1, 'painting:balloon', 50, now=100, seed=5)
        room = self.repo.begin(room['id'], 1, [self.participant(1)], now=110)
        party = {'1': {'hp': 500, 'max_hp': 1000, 'fallen': False}}
        battle = {'round': 7, 'log': ['完成']}
        settled = self.repo.settle_painting(
            room['id'], 1, 0, '勝利', battle, party, now=120)
        self.assertEqual((settled['boss_index'], len(settled['battle_history'])), (1, 1))
        retried = self.repo.settle_painting(
            room['id'], 1, 0, '勝利', battle, party, now=121)
        self.assertEqual((retried['boss_index'], len(retried['battle_history'])), (1, 1))
        with self.assertRaisesRegex(PaintedMazeError, '不同結果'):
            self.repo.settle_painting(
                room['id'], 1, 0, '戰敗', battle, party, now=121)

        self.repo.close(room['id'], 99, administrator=True, now=130)
        failed_room = self.repo.create(1, 1, 'painting:balloon', 50, now=200, seed=6)
        failed_room = self.repo.begin(failed_room['id'], 1, [self.participant(1)], now=210)
        failed = self.repo.settle_painting(
            failed_room['id'], 1, 0, '戰敗', {'round': 4},
            {'1': {'hp': 0, 'max_hp': 1000, 'fallen': True}}, now=220)
        self.assertEqual((failed['status'], failed['boss_index']), ('failed', 0))
        self.assertTrue(failed['entry_consumed'])


if __name__ == '__main__':
    unittest.main()
