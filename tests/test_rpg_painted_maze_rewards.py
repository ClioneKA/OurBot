from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_crystals import CrystalStore
from core.rpg_painted_maze import PaintedMazeStore
from core.rpg_painted_maze_rewards import PaintedMazeRewardStore
from core.rpg_painted_maze_service import PaintedMazeService
from core.settings import RPGSettings


class PaintedMazeRewardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rpg = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.addCleanup(self.rpg.close)
        self.characters = Characters(self.rpg, RPGSettings())
        self.maze = PaintedMazeStore(self.rpg)
        self.crystals = CrystalStore(self.rpg)
        self.rewards = PaintedMazeRewardStore(self.rpg)
        for user_id in (1, 2):
            self.characters.create(1, user_id)

    @staticmethod
    def participant(user_id, job):
        return {'id': user_id, 'state': {'level': 60, 'job': job,
                'combat': {'HP': 1000}}, 'rules': [], 'passive_id': None}

    def completed_room(self, entry_item, seed, *, now=100):
        self.characters.grant_item(1, 1, entry_item)
        room = self.maze.create(1, 1, entry_item, 60, now=now, seed=seed)
        self.maze.change_member(room['id'], 2, 60, now=now + 1)
        room = self.maze.begin(room['id'], 1, [
            self.participant(1, '弓兵'), self.participant(2, '僧侶')], now=now + 2)
        clock = now + 3
        for _stage in range(3):
            for _boss in range(3):
                room = self.maze.record_boss_victory(room['id'], 1, now=clock)
                clock += 1
            room = self.maze.resolve_contract(
                room['id'], now=room['contract_vote']['deadline'])
            clock = room['last_contract_vote']['deadline'] + 1
        return self.maze.settle_final(
            room['id'], 1, '勝利', {'round': 12},
            {'1': {'hp': 500}, '2': {'hp': 600}}, now=clock)

    def test_noah_first_clear_is_choice_and_retry_does_not_duplicate(self):
        room = self.completed_room('noah:unfinished', 101)
        first = self.rewards.seal_noah_clear(room['id'], now=500)
        self.assertEqual([reward['status'] for reward in first], ['pending', 'pending'])
        self.assertEqual(first[0]['choices'], ['maze:archer:weapon', 'maze:archer:suit'])
        self.assertEqual(first, self.rewards.seal_noah_clear(room['id'], now=999))

        equipment_id = self.rewards.choose_noah_gear(
            room['id'], 1, 1, 'maze:archer:suit', now=501)
        self.assertEqual(self.characters.get_instance(1, 1, equipment_id).item_id,
                         'maze:archer:suit')
        self.assertEqual(self.rewards.choose_noah_gear(
            room['id'], 1, 1, 'maze:archer:suit', now=999), equipment_id)
        self.assertEqual(len(self.characters.equipment_instances(1, 1)), 2)  # starter + reward

    def test_second_noah_clear_grants_deterministic_random_job_gear(self):
        first_room = self.completed_room('noah:unfinished', 201)
        self.rewards.seal_noah_clear(first_room['id'])
        self.rewards.choose_noah_gear(first_room['id'], 1, 1, 'maze:archer:weapon')
        self.rewards.choose_noah_gear(first_room['id'], 1, 2, 'maze:monk:weapon')

        second_room = self.completed_room('noah:unfinished', 202, now=1000)
        result = self.rewards.seal_noah_clear(second_room['id'], now=1500)
        self.assertEqual([reward['status'] for reward in result], ['granted', 'granted'])
        self.assertIn(result[0]['item_id'], ('maze:archer:weapon', 'maze:archer:suit'))
        instance_id = result[0]['equipment_instance_id']
        retried = self.rewards.seal_noah_clear(second_room['id'], now=1900)
        self.assertEqual(retried[0]['equipment_instance_id'], instance_id)

    def test_shadow_clear_grants_three_extra_crystals_per_member_once(self):
        room = self.completed_room('painting:balloon', 301)
        first = self.crystals.seal_shadow_bonus(room['id'], now=500)
        second = self.crystals.seal_shadow_bonus(room['id'], now=999)
        self.assertEqual([crystal.instance_id for crystal in first],
                         [crystal.instance_id for crystal in second])
        self.assertEqual(len(first), 6)
        self.assertEqual(len(self.crystals.inventory(1, 1)), 3)
        self.assertEqual(len(self.crystals.inventory(1, 2)), 3)

    def test_final_settlement_is_idempotent(self):
        room = self.completed_room('painting:balloon', 401)
        retried = self.maze.settle_final(
            room['id'], 1, '勝利', {'round': 12}, {'1': {'hp': 500}}, now=999)
        self.assertEqual((retried['status'], retried['final_battle']['result']),
                         ('completed', '勝利'))
        with self.assertRaisesRegex(Exception, '不同結果'):
            self.maze.settle_final(
                room['id'], 1, '戰敗', {'round': 12}, {'1': {'hp': 0}}, now=1000)

    def test_reward_outbox_recovers_all_checkpoints_once_after_restart(self):
        room = self.completed_room('painting:balloon', 501)
        service = PaintedMazeService.__new__(PaintedMazeService)
        service.repo, service.crystals, service.rewards = self.maze, self.crystals, self.rewards
        recovered = service.recover_rewards(room['id'])
        self.assertEqual(recovered['reward_due'], [])
        self.assertEqual(len(self.crystals.inventory(1, 1)), 6)
        self.assertEqual((self.rpg.xp(1, 1), self.rpg.gold(1, 1)), (2050, 1250))
        equipment_before = len(self.characters.equipment_instances(1, 1))
        service.recover_rewards(room['id'])
        self.assertEqual(len(self.crystals.inventory(1, 1)), 6)
        self.assertEqual((self.rpg.xp(1, 1), self.rpg.gold(1, 1)), (2050, 1250))
        self.assertEqual(len(self.characters.equipment_instances(1, 1)), equipment_before)


if __name__ == '__main__':
    unittest.main()
