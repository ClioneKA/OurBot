from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sqlite3
from core.memory import MemoryStore
from core.rpg_affinity import hanna_affinity, tailoring_price

from core.rpg import RPGStore
from core.rpg_character import Characters, CharacterError
from core.rpg_commissions import DailyCommissions, commission_day
from core.rpg_battle import dump_battle, raid_battle
from core.rpg_monsters import prepare_monster
from core.rpg_raid_store import RaidStore
from core.settings import RPGSettings


class DailyCommissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.store.create_player(1, 1)
        self.memory = MemoryStore(str(Path(self.directory.name) / 'memory.db'))
        self.quests = DailyCommissions(self.store, self.memory)
        self.now = datetime(2026, 9, 10, 8, tzinfo=timezone.utc).timestamp()
        self.day, self.board = self.quests.board(1, 1, self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def stock(self, quantity):
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO rpg_inventory VALUES (1,1,?,?)',
                                 (self.board['hanna']['target'], quantity))

    def test_shared_board_survives_restart(self):
        self.assertEqual(self.quests.board(1, 2, self.now), (self.day, self.board))
        self.store.close()
        self.store = RPGStore(self.path)
        self.quests = DailyCommissions(self.store)
        self.assertEqual(self.quests.board(1, 1, self.now), (self.day, self.board))

    def test_delivery_is_exact_and_cannot_claim_twice(self):
        required = self.board['hanna']['quantity']
        self.stock(required + 2)
        before = self.store.gold(1, 1)
        gain = self.board['hanna']['affinity']
        self.assertEqual(self.quests.claim(1, 1, self.day, 'hanna', self.now), dict(score=gain, delta=gain))
        with self.assertRaises(CharacterError):
            self.quests.claim(1, 1, self.day, 'hanna', self.now)
        self.assertEqual(self.store.gold(1, 1), before)
        self.assertEqual(hanna_affinity(self.store.db, 1, 1), gain)
        quest = self.quests.board(1, 1, self.now)[1]['hanna']
        self.assertEqual(quest['progress'], 2)
        self.assertTrue(quest['claimed'])

    def test_insufficient_food_leaves_wallet_inventory_and_claim_unchanged(self):
        self.stock(1)
        before = self.store.gold(1, 1)
        with self.assertRaisesRegex(CharacterError, '不足'):
            self.quests.claim(1, 1, self.day, 'hanna', self.now)
        quest = self.quests.board(1, 1, self.now)[1]['hanna']
        self.assertEqual(quest['progress'], 1)
        self.assertFalse(quest['claimed'])
        self.assertEqual(self.store.gold(1, 1), before)

    def test_midnight_rejects_stale_buttons_and_resets_claims(self):
        midnight = datetime(2026, 9, 10, 16, tzinfo=timezone.utc).timestamp()
        self.assertEqual(commission_day(midnight - 1), '2026-09-10')
        self.assertEqual(commission_day(midnight), '2026-09-11')
        self.stock(10)
        self.quests.claim(1, 1, self.day, 'hanna', midnight - 1)
        with self.assertRaisesRegex(CharacterError, '已更新'):
            self.quests.claim(1, 1, self.day, 'hanna', midnight)
        day, board = self.quests.board(1, 1, midnight)
        self.assertNotEqual(day, self.day)
        self.assertFalse(board['hanna']['claimed'])
        self.assertEqual(board['annan']['progress'], 0)

    def settle(self, result='勝利', source=None, kind=None):
        repo = RaidStore(self.store)
        monster = prepare_monster(dict(kind=kind or self.board['annan']['target'],
                                       name='測試魔物', description='測試'), quality='普通')
        raid = repo.create(1, 2, monster, self.now, dict(victory_xp=100, victory_gold=0, drop_chance=0))
        participant = dict(id=1, name='玩家', state=self.characters.snapshot(1, 1), rules=[])
        raid.update(status='running', participants=[participant], members=[1], source=source)
        repo.save(raid)
        battle = raid_battle([participant], raid['monster'], raid['seed'])
        battle.result = result
        with patch('core.rpg_commissions.time.time', return_value=self.now):
            repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
            repo.settle(raid['id'], dump_battle(battle), self.settings.raid)

    def test_raid_settlement_counts_once_and_rewards_only_participants(self):
        self.settle(source='bounty')
        self.assertEqual(self.quests.board(1, 1, self.now)[1]['annan']['progress'], 1)
        self.assertEqual(self.quests.claim(1, 1, self.day, 'annan', self.now), dict(score=1, delta=1))
        self.assertEqual(self.memory.get_affinity(1, 1), 1)
        with self.assertRaises(CharacterError):
            self.quests.claim(1, 1, self.day, 'annan', self.now)
        self.assertEqual(self.quests.board(1, 2, self.now)[1]['annan']['progress'], 0)
        self.assertEqual(self.quests.board(2, 1, self.now)[1]['annan']['progress'], 0)

    def test_defeat_admin_and_wrong_monster_do_not_complete_quest(self):
        self.settle(result='敗北')
        self.settle(source='admin')
        other = '毒蛛' if self.board['annan']['target'] != '毒蛛' else '巨獸'
        self.settle(kind=other)
        self.assertEqual(self.quests.board(1, 1, self.now)[1]['annan']['progress'], 0)
        with self.assertRaisesRegex(CharacterError, '尚未達成'):
            self.quests.claim(1, 1, self.day, 'annan', self.now)

    def test_annan_ignores_chat_limit_and_keeps_chat_counter(self):
        self.memory.apply_daily_affinity_delta(1, 1, 2, self.day, 1)
        self.settle()
        self.quests.claim(1, 1, self.day, 'annan', self.now)
        self.assertEqual(self.memory.get_affinity(1, 1), 3)
        self.assertEqual(self.memory.apply_daily_affinity_delta(1, 1, 2, self.day, 1), 3)

    def test_existing_annan_affinity_caps_at_100(self):
        self.memory.set_affinity(1, 1, 100)
        self.settle()
        self.assertEqual(self.quests.claim(1, 1, self.day, 'annan', self.now), dict(score=100, delta=0))

    def test_legacy_gold_board_and_claim_migrate_without_a_second_reward(self):
        import json
        legacy = dict(annan=dict(target='巨獸', quantity=1, gold=300),
                      hanna=dict(target='farming:potato', quantity=2, gold=200))
        with self.store.db:
            self.store.db.execute('UPDATE rpg_daily_commission_boards SET data=?', (json.dumps(legacy),))
            self.store.db.execute('DROP TABLE rpg_daily_commission_claims')
            self.store.db.execute('''CREATE TABLE rpg_daily_commission_claims (
                guild_id INTEGER, user_id INTEGER, day TEXT, npc TEXT,
                PRIMARY KEY(guild_id,user_id,day,npc))''')
            self.store.db.execute('INSERT INTO rpg_daily_commission_claims VALUES (1,1,?,?)', (self.day, 'annan'))
        migrated = DailyCommissions(self.store, self.memory)
        board = migrated.board(1, 1, self.now)[1]
        self.assertTrue(board['annan']['claimed'])
        self.assertFalse(board['annan']['pending'])
        self.assertNotIn('gold', board['hanna'])
        self.assertEqual(board['hanna']['affinity'], 1)
        self.assertEqual(self.memory.get_affinity(1, 1), 0)

    def test_pending_reward_retries_after_cross_database_failure_without_double_gain(self):
        self.settle()
        original = self.memory.award_commission_affinity
        def fail_after_commit(*args):
            original(*args)
            raise sqlite3.OperationalError('connection lost after commit')
        with patch.object(self.memory, 'award_commission_affinity', side_effect=fail_after_commit):
            with self.assertRaisesRegex(CharacterError, '已保留'):
                self.quests.claim(1, 1, self.day, 'annan', self.now)
        self.assertEqual(self.memory.get_affinity(1, 1), 1)
        restarted = DailyCommissions(self.store, self.memory)
        restarted.board(1, 1, self.now + 86400)
        self.assertEqual(self.memory.get_affinity(1, 1), 1)
        self.assertEqual(self.store.db.execute('SELECT delivered FROM rpg_daily_commission_claims').fetchone()[0], 1)

    def test_hanna_quality_rewards_cap_and_prices(self):
        from core.rpg_provisions import INGREDIENTS
        from core.rpg_commissions import FOOD_TARGETS
        import json
        for quality in range(1, 6):
            target = next(key for key in FOOD_TARGETS if INGREDIENTS[key].quality == quality)
            self.board['hanna'].update(target=target, affinity=quality)
            with self.store.db:
                self.store.db.execute('UPDATE rpg_daily_commission_boards SET data=?', (json.dumps(self.board),))
                self.store.db.execute('DELETE FROM rpg_daily_commission_claims')
                self.store.db.execute('DELETE FROM rpg_hanna_affinity')
            self.stock(10)
            self.assertEqual(self.quests.claim(1, 1, self.day, 'hanna', self.now)['delta'], quality)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_hanna_affinity SET score=99')
            self.store.db.execute('DELETE FROM rpg_daily_commission_claims')
        self.assertEqual(self.quests.claim(1, 1, self.day, 'hanna', self.now), dict(score=100, delta=1))
        self.assertEqual(tailoring_price(self.store.db, 1, 1, 1000), 750)
        self.assertEqual(tailoring_price(self.store.db, 1, 1, 500), 375)
        self.assertEqual(tailoring_price(self.store.db, 2, 1, 1000), 1000)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_hanna_affinity SET score=1')
        self.assertEqual(tailoring_price(self.store.db, 1, 1, 500), 499)


if __name__ == '__main__':
    unittest.main()
