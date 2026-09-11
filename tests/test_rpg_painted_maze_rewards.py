from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_crystals import CrystalStore
from core.rpg_painted_maze import PaintedMazeStore
from core.rpg_painted_maze_rewards import PaintedMazeRewardStore
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

    def completed_room(self, entry_item, seed, *, now=100, require_entry=True):
        if require_entry:
            self.characters.grant_item(1, 1, entry_item)
        room = self.maze.create(1, 1, entry_item, 60, now=now, seed=seed,
                                require_entry=require_entry)
        self.maze.change_member(room['id'], 2, 60, now=now + 1)
        room = self.maze.begin(room['id'], 1, [
            self.participant(1, '弓兵'), self.participant(2, '僧侶')], now=now + 2)
        clock = now + 3
        for _stage in range(3):
            room = self.maze.record_boss_victory(room['id'], 1, now=clock)
            clock += 1
            room = self.maze.resolve_contract(
                room['id'], now=room['contract_vote']['deadline'])
            clock = room['last_contract_vote']['deadline'] + 1
        return self.maze.settle_final(
            room['id'], 1, '勝利', {'round': 12},
            {'1': {'hp': 500}, '2': {'hp': 600}}, now=clock)

    def test_admin_rooms_grant_nothing_on_both_routes_or_recovery(self):
        from types import SimpleNamespace
        from core.rpg_painted_maze_service import PaintedMazeService

        service = PaintedMazeService(SimpleNamespace(store=self.rpg, bot=None))
        tables = ('players', 'rpg_wallets', 'rpg_inventory', 'rpg_crystal_instances',
                  'rpg_painted_maze_noah_clears', 'rpg_painted_maze_final_rewards',
                  'rpg_painted_maze_currency_rewards', 'rpg_painted_maze_accessories')
        before = {table: self.rpg.db.execute(f'SELECT * FROM {table}').fetchall()
                  for table in tables}
        for index, entry in enumerate(('noah:unfinished', 'painting:balloon')):
            room = self.completed_room(entry, 900 + index, now=100 + index * 1000,
                                       require_entry=False)
            for stage in (1, 2, 3):
                self.assertEqual(self.crystals.seal_stage(room['id'], stage), [])
            for checkpoint in (1, 2, 3, 4):
                self.assertEqual(self.rewards.seal_currency(room['id'], checkpoint), [])
            if index == 0:
                self.assertEqual(self.rewards.seal_noah_clear(room['id']), [])
            else:
                self.assertEqual(self.crystals.seal_shadow_bonus(room['id']), [])
            for _ in range(2):
                recovered = service.recover_rewards(room['id'])
                self.assertEqual(recovered['reward_due'], [])
            embed = service.room_embed(recovered)
            self.assertIn('無獎勵測試', [field.name for field in embed.fields])
        for table in tables:
            self.assertEqual(self.rpg.db.execute(f'SELECT * FROM {table}').fetchall(),
                             before[table], table)
        regular = self.completed_room('noah:unfinished', 902, now=3000)
        self.assertTrue(all(row['status'] == 'box_granted'
                            for row in self.rewards.seal_noah_clear(regular['id'])))

    def test_noah_first_clear_is_choice_and_retry_does_not_duplicate(self):
        room = self.completed_room('noah:unfinished', 101)
        first = self.rewards.seal_noah_clear(room['id'], now=500)
        self.assertEqual([reward['status'] for reward in first],
                         ['box_granted', 'box_granted'])
        self.assertEqual(first[0]['choices'], ['maze:archer:weapon', 'maze:archer:suit'])
        self.assertEqual(first, self.rewards.seal_noah_clear(room['id'], now=999))
        self.assertEqual(self.characters.inventory_counts(1, 1)['maze:choice_box:archer'], 1)

        item_id, equipment_id = self.rewards.open_choice_box(
            1, 1, 'maze:choice_box:archer', 'suit')
        self.assertEqual(item_id, 'maze:archer:suit')
        self.assertEqual(self.characters.get_instance(1, 1, equipment_id).item_id,
                         'maze:archer:suit')
        with self.assertRaisesRegex(Exception, '沒有這個'):
            self.rewards.open_choice_box(1, 1, 'maze:choice_box:archer', 'weapon')
        self.assertEqual(len(self.characters.equipment_instances(1, 1)), 2)  # starter + reward

    def test_second_noah_clear_grants_deterministic_random_job_gear(self):
        first_room = self.completed_room('noah:unfinished', 201)
        self.rewards.seal_noah_clear(first_room['id'])
        self.rewards.open_choice_box(1, 1, 'maze:choice_box:archer', 'weapon')
        self.rewards.open_choice_box(1, 2, 'maze:choice_box:monk', 'weapon')

        second_room = self.completed_room('noah:unfinished', 202, now=1000)
        result = self.rewards.seal_noah_clear(second_room['id'], now=1500)
        self.assertEqual([reward['status'] for reward in result], ['granted', 'granted'])
        self.assertIn(result[0]['item_id'], ('maze:archer:weapon', 'maze:archer:suit'))
        instance_id = result[0]['equipment_instance_id']
        retried = self.rewards.seal_noah_clear(second_room['id'], now=1900)
        self.assertEqual(retried[0]['equipment_instance_id'], instance_id)

    def test_legacy_pending_choice_is_converted_to_one_inventory_box(self):
        room = self.completed_room('noah:unfinished', 250)
        self.rewards.seal_noah_clear(room['id'], now=500)
        with self.rpg.db:
            self.rpg.db.execute('''DELETE FROM rpg_inventory
                WHERE guild_id=1 AND user_id=1 AND item_id='maze:choice_box:archer' ''')
            self.rpg.db.execute('''UPDATE rpg_painted_maze_final_rewards
                SET status='pending',item_id=NULL,claimed_at=NULL
                WHERE room_id=? AND user_id=1''', (room['id'],))
        self.assertEqual(self.rewards.release_pending_boxes(1, 1),
                         ['maze:choice_box:archer'])
        self.assertEqual(self.rewards.release_pending_boxes(1, 1), [])
        self.assertEqual(self.characters.inventory_counts(1, 1)['maze:choice_box:archer'], 1)

    def test_shadow_clear_grants_three_extra_crystals_per_member_once(self):
        room = self.completed_room('painting:balloon', 301)
        first = self.crystals.seal_shadow_bonus(room['id'], now=500)
        second = self.crystals.seal_shadow_bonus(room['id'], now=999)
        self.assertEqual([crystal.instance_id for crystal in first],
                         [crystal.instance_id for crystal in second])
        self.assertEqual(len(first), 6)
        self.assertEqual(len(self.crystals.inventory(1, 1)), 3)
        self.assertEqual(len(self.crystals.inventory(1, 2)), 3)

    def test_shadow_first_clear_accessory_upgrades_after_noah_and_keeps_instance(self):
        shadow = self.completed_room('painting:balloon', 350)
        first = self.rewards.seal_route_accessory(shadow['id'], now=500)
        retried = self.rewards.seal_route_accessory(shadow['id'], now=600)
        self.assertEqual(first, retried)
        self.assertTrue(all(row['item_id'] == 'maze:shadow:emblem' for row in first))
        instance_id = first[0]['instance_id']

        noah = self.completed_room('noah:unfinished', 351, now=1000)
        upgraded = self.rewards.seal_route_accessory(noah['id'], now=1500)
        self.assertTrue(all(row['item_id'] == 'maze:shadow:radiance' for row in upgraded))
        self.assertEqual(upgraded[0]['instance_id'], instance_id)
        self.assertEqual(self.characters.get_instance(1, 1, instance_id).item_id,
                         'maze:shadow:radiance')

    def test_noah_before_shadow_grants_radiance_directly_and_admin_grants_nothing(self):
        noah = self.completed_room('noah:unfinished', 360)
        pending = self.rewards.seal_route_accessory(noah['id'])
        self.assertTrue(all(row['item_id'] is None for row in pending))
        shadow = self.completed_room('painting:balloon', 361, now=1000)
        final = self.rewards.seal_route_accessory(shadow['id'])
        self.assertTrue(all(row['item_id'] == 'maze:shadow:radiance' for row in final))
        practice = self.completed_room('painting:balloon', 362, now=2000, require_entry=False)
        self.assertEqual(self.rewards.seal_route_accessory(practice['id']), [])

    def test_legacy_shadow_clear_is_backfilled_when_noah_route_completes(self):
        shadow = self.completed_room('painting:balloon', 370)
        self.crystals.seal_shadow_bonus(shadow['id'])
        noah = self.completed_room('noah:unfinished', 371, now=1000)
        result = self.rewards.seal_route_accessory(noah['id'])
        self.assertTrue(all(row['item_id'] == 'maze:shadow:radiance' for row in result))
        saved = self.rpg.db.execute('''SELECT shadow_room_id,noah_room_id,upgraded
            FROM rpg_painted_maze_accessories WHERE guild_id=1 AND user_id=1''').fetchone()
        self.assertEqual(saved, (shadow['id'], noah['id'], 1))

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
        from core.rpg_painted_maze_service import PaintedMazeService
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
