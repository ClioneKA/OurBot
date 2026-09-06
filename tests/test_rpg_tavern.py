from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord

from core.rpg import RPGStore
from core.rpg_battle import dump_battle, raid_battle
from core.rpg_character import CharacterError, Characters
from core.rpg_raids import RaidService
from core.rpg_raid_store import RaidStore
from core.rpg_monsters import prepare_monster
from core.rpg_tavern import BOUNTY_PRICES, DRINK_PACKAGES, DRINK_XP_PERCENT, TavernStore
from core.settings import RPGSettings


class TavernStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.directory.name) / 'rpg.db')
        self.tavern = TavernStore(self.store)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,10000)')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_drink_packages_scale_to_twenty_guests(self):
        self.assertEqual([(item.price, item.capacity) for item in DRINK_PACKAGES.values()],
                         [(500, 5), (1000, 10), (2000, 20)])

    def test_round_deducts_claims_once_and_is_consumed_by_next_raid(self):
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.assertEqual(self.store.gold(1, 1), 9500)
        self.tavern.publish_offer(offer['id'], 99)
        self.tavern.claim(offer['id'], 1, 2, now=101)
        with self.assertRaises(CharacterError):
            self.tavern.claim(offer['id'], 1, 2, now=102)
        self.assertEqual(self.tavern.prepare_for_raid('raid-a', 1, [2], now=200),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})
        self.assertEqual(self.tavern.prepare_for_raid('raid-a', 1, [2], now=200),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})
        self.assertEqual(self.tavern.prepare_for_raid('raid-b', 1, [2], now=201), {})

    def test_unpublished_round_is_refunded_once_during_recovery(self):
        self.tavern.create_offer(1, 1, 9, 'hall', now=100)
        self.assertEqual(self.store.gold(1, 1), 9000)
        self.tavern.recover_drafts()
        self.tavern.recover_drafts()
        self.assertEqual(self.store.gold(1, 1), 10000)

    def test_drink_bonus_is_included_in_settlement_xp(self):
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.tavern.publish_offer(offer['id'], 99)
        self.tavern.claim(offer['id'], 1, 2, now=101)
        settings = RPGSettings()
        characters = Characters(self.store, settings)
        participant = dict(id=2, name='玩家', state=characters.snapshot(1, 2), rules=[])
        repo = RaidStore(self.store)
        monster = prepare_monster(dict(kind='巨獸', name='測試巨獸', description='測試'), quality='普通')
        raid = repo.create(1, 2, monster, 200,
                           dict(victory_xp=100, victory_gold=0, drop_chance=0.0), use_dynamic=False)
        participant['tavern'] = self.tavern.prepare_for_raid(raid['id'], 1, [2], now=200)[2]
        raid.update(status='running', participants=[participant], members=[2])
        repo.save(raid)
        battle = raid_battle([participant], raid['monster'], raid['seed'])
        battle.result = '勝利'
        result = repo.settle(raid['id'], dump_battle(battle), settings.raid)
        self.assertEqual(result['rewards'][0]['xp'], 105)
        self.assertEqual(self.store.xp(1, 2), 105)


class TavernBountyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.directory.name) / 'rpg.db')
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.channel = self.FakeChannel()
        bot = SimpleNamespace(get_channel=lambda channel_id: self.channel if channel_id == 2 else None)
        cog = SimpleNamespace(store=self.store, settings=self.settings, characters=self.characters,
                              bot=bot, ai_model='test')
        self.service = None
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,10000)')
        with patch.dict('os.environ', {'RPG_RAID_CHANNEL_IDS': '2', 'RPG_MID_RAID_CHANNEL_IDS': ''}):
            self.service = RaidService(cog)
        self.service.notifications.ensure = AsyncMock(return_value=None)

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.directory.cleanup()

    class FakeChannel:
        id = 2
        mention = '<#2>'
        guild = SimpleNamespace(id=1, unavailable=False)

        def __init__(self):
            self.send = AsyncMock(return_value=SimpleNamespace(id=99))

    async def test_bounty_has_no_gold_or_dynamic_scaling_and_preserves_schedule(self):
        self.service.repo.schedule(2, 12345)
        user = SimpleNamespace(id=1, bot=False)
        self.service.imagine = AsyncMock(return_value={
            'kind': '巨獸', 'name': '酒館巨獸', 'description': '測試懸賞'})
        with patch('core.rpg_raids.discord.TextChannel', self.FakeChannel), \
                patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                patch('core.rpg_monsters.random.choices', return_value=['普通']):
            channel, _, raid = await self.service.summon_bounty(
                self.channel.guild, user, 'regular', BOUNTY_PRICES['regular'])
        self.assertIs(channel, self.channel)
        self.assertEqual(self.store.gold(1, 1), 8000)
        self.assertEqual(self.service.repo.next_at(2), 12345)
        self.assertEqual((raid['reward_policy']['victory_gold'], raid['members']), (0, [1]))
        self.assertTrue(raid['preserve_schedule'])
        self.assertNotIn('difficulty', raid)
        self.assertTrue(self.service.lobby_embed(raid).title.startswith('酒館懸賞'))


if __name__ == '__main__':
    unittest.main()
