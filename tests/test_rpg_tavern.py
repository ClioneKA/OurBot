from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord

from core.rpg import RPGStore
from core.rpg_battle import dump_battle, raid_battle
from core.rpg_character import CharacterError, Characters
from core.rpg_provisions import Provisions
from core.rpg_raids import RaidService
from core.rpg_raid_store import RaidStore
from core.rpg_monsters import prepare_monster
from core.rpg_tavern import (BOUNTY_PRICES, DRINK_CLAIM_SECONDS, DRINK_PACKAGES,
                             DRINK_XP_PERCENT, DrinkOfferView, TavernService,
                             TavernStore, TavernView)
from core.rpg_spaces import AdventureSpace
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
        self.assertEqual((DRINK_CLAIM_SECONDS, offer['expires_at']), (7200, 7300))
        self.assertEqual(self.store.gold(1, 1), 9500)
        self.tavern.publish_offer(offer['id'], 99)
        self.tavern.claim(offer['id'], 1, 2, now=101)
        active = self.tavern.active_effect(1, 2, now=102)
        self.assertEqual((active['name'], active['xp_percent'], active['valid_until']),
                         (DRINK_PACKAGES['table'].name, DRINK_XP_PERCENT, 86501))
        with self.assertRaises(CharacterError):
            self.tavern.claim(offer['id'], 1, 2, now=102)
        self.assertEqual(self.tavern.prepare_for_raid('raid-a', 1, [2], now=200),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})
        self.assertEqual(self.tavern.prepare_for_raid('raid-a', 1, [2], now=200),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})
        self.assertEqual(self.tavern.prepare_for_raid('raid-b', 1, [2], now=201), {})
        self.assertIsNone(self.tavern.active_effect(1, 2, now=201))

    def test_consumed_guest_can_claim_same_open_round_again(self):
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.tavern.publish_offer(offer['id'], 99)
        self.tavern.claim(offer['id'], 1, 2, now=101)
        self.assertEqual(self.tavern.prepare_for_raid('raid-a', 1, [2], now=102),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})

        self.tavern.claim(offer['id'], 1, 2, now=103)

        self.assertEqual(self.tavern.claim_count(offer['id']), 2)
        self.assertEqual(self.tavern.claimants(offer['id']), [2, 2])
        self.assertEqual(self.tavern.prepare_for_raid('raid-b', 1, [2], now=104),
                         {2: {'xp_percent': DRINK_XP_PERCENT}})

    def test_round_preserves_complete_guest_list(self):
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.tavern.publish_offer(offer['id'], 99)
        for user_id in (4, 2, 3):
            self.tavern.claim(offer['id'], 1, user_id, now=100 + user_id)

        self.assertEqual(self.tavern.claimants(offer['id']), [2, 3, 4])
        view = DrinkOfferView(SimpleNamespace(store=self.tavern), offer['id'])
        self.addCleanup(view.stop)
        self.assertIn('1. <@2>\n2. <@3>\n3. <@4>', view.embed().fields[0].value)

    def test_repeat_claim_uses_capacity(self):
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.tavern.publish_offer(offer['id'], 99)
        self.tavern.claim(offer['id'], 1, 2, now=101)
        self.tavern.prepare_for_raid('raid-a', 1, [2], now=102)
        self.tavern.claim(offer['id'], 1, 2, now=103)
        for user_id in (3, 4, 5):
            self.tavern.claim(offer['id'], 1, user_id, now=103 + user_id)

        with self.assertRaisesRegex(CharacterError, '客滿'):
            self.tavern.claim(offer['id'], 1, 6, now=110)

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

    def test_drink_and_meal_can_be_held_at_the_same_time(self):
        characters = Characters(self.store, RPGSettings())
        provisions = Provisions(self.store)
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        for user in (2, 3):
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO rpg_inventory VALUES (1,?,'fishing:pond:common',3)", (user,))
                self.store.db.execute(
                    "INSERT INTO rpg_inventory VALUES (1,?,'farming:potato',2)", (user,))
        offer = self.tavern.create_offer(1, 1, 9, 'table', now=100)
        self.tavern.publish_offer(offer['id'], 99)

        self.tavern.claim(offer['id'], 1, 2, now=101)
        meal_after_drink = provisions.cook(1, 2, 9, ingredients, now=102)
        self.assertEqual(provisions.claimants(meal_after_drink['id']), [2])

        meal_before_drink = provisions.cook(1, 3, 9, ingredients, now=102)
        self.assertEqual(provisions.claimants(meal_before_drink['id']), [3])
        self.tavern.claim(offer['id'], 1, 3, now=103)
        self.assertEqual(self.tavern.claimants(offer['id']), [2, 3])


class DedicatedTavernChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.directory.name) / 'rpg.db')
        self.public = SimpleNamespace(id=88, send=AsyncMock(
            return_value=SimpleNamespace(id=99)))
        self.current = SimpleNamespace(id=77, send=AsyncMock())
        self.guild = SimpleNamespace(id=1, get_channel=lambda channel_id:
                                     self.public if channel_id == 88 else None)
        self.interaction = SimpleNamespace(
            guild_id=1, guild=self.guild, channel=self.current,
            user=SimpleNamespace(id=1, bot=False))
        self.characters = Characters(self.store, RPGSettings())
        self.provisions = Provisions(self.store)
        self.cog = SimpleNamespace(store=self.store, characters=self.characters,
                                   provisions=self.provisions)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,10000)')
        with patch.dict('os.environ', {'RPG_TAVERN_CHANNEL_IDS': '88'}):
            self.service = TavernService(self.cog)
        self.cog.tavern = self.service

    async def asyncTearDown(self):
        self.service.close()
        self.store.close()
        self.directory.cleanup()

    async def test_round_is_posted_to_configured_channel_not_current_channel(self):
        message, offer = await self.service.buy_round(self.interaction, 'table')

        self.assertEqual(message.id, 99)
        self.assertEqual(offer['channel_id'], 88)
        self.public.send.assert_awaited_once()
        self.current.send.assert_not_awaited()

    async def test_database_managed_tavern_takes_priority_over_environment(self):
        managed = SimpleNamespace(id=89, send=AsyncMock(return_value=SimpleNamespace(id=100)))
        original_get_channel = self.guild.get_channel
        self.guild.get_channel = lambda channel_id: managed if channel_id == 89 else original_get_channel(channel_id)
        self.cog.spaces = SimpleNamespace(store=SimpleNamespace(
            get=lambda guild_id: AdventureSpace(guild_id, tavern_channel_id=89)))
        message, offer = await self.service.buy_round(self.interaction, 'table')
        self.assertEqual((message.id, offer['channel_id']), (100, 89))
        managed.send.assert_awaited_once()
        self.public.send.assert_not_awaited()

    async def test_missing_guild_channel_rejects_without_charging(self):
        self.guild.get_channel = lambda channel_id: None

        with self.assertRaisesRegex(CharacterError, '酒館公開頻道'):
            await self.service.buy_round(self.interaction, 'table')
        self.assertEqual(self.store.gold(1, 1), 10000)

    async def test_meal_is_posted_to_configured_channel_not_current_channel(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'fishing:pond:common',3)")
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'farming:potato',2)")

        message, meal = await self.service.serve_meal(
            self.interaction,
            ['fishing:pond:common'] * 3 + ['farming:potato'] * 2)

        self.assertEqual(message.id, 99)
        self.assertEqual(meal['channel_id'], 88)
        self.public.send.assert_awaited_once()
        self.current.send.assert_not_awaited()

    async def test_aftertaste_no_longer_reduces_seats_and_meal_is_posted(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'fishing:waterway:rare',4)")
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'cooking:seasoning:high',1)")

        message, meal = await self.service.serve_meal(
            self.interaction,
            ['fishing:waterway:rare'] * 4 + ['cooking:seasoning:high'])

        self.assertEqual(message.id, 99)
        self.assertEqual((meal['capacity'], meal['data']['duration'], meal['status']),
                         (meal['data']['total_portions'], 3, 'open'))
        self.public.send.assert_awaited_once()
        self.current.send.assert_not_awaited()

    async def test_full_offers_are_deleted_and_claimed_effects_remain(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'fishing:pond:common',3)")
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'farming:potato',2)")
        _, drink = await self.service.buy_round(self.interaction, 'table')
        _, meal = await self.service.serve_meal(self.interaction,
            ['fishing:pond:common'] * 3 + ['farming:potato'] * 2)
        for offer, view, key, existing in (
            (drink, self.service.offer_view(drink['id']), drink['id'], 0),
            (meal, self.service.meal_view(meal['id']), f'meal:{meal["id"]}', 1),
        ):
            for index in range(offer['capacity'] - existing):
                user_id = index + 2
                self.store.create_player(1, user_id)
                interaction = SimpleNamespace(
                    guild_id=1, user=SimpleNamespace(id=user_id, bot=False),
                    response=SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock(),
                                             send_message=AsyncMock()),
                    delete_original_response=AsyncMock(), edit_original_response=AsyncMock())
                async def before_delete(seconds):
                    self.assertEqual(seconds, 5)
                    interaction.edit_original_response.assert_awaited_once()
                    self.assertIsNone(interaction.edit_original_response.call_args.kwargs['view'])
                    interaction.delete_original_response.assert_not_awaited()
                with patch('core.rpg_tavern.asyncio.sleep', side_effect=before_delete) as delay:
                    await view.claim_button.callback(interaction)
                interaction.response.send_message.assert_not_awaited()
                if index + existing + 1 == offer['capacity']:
                    delay.assert_awaited_once_with(5)
                    interaction.response.defer.assert_awaited_once()
                    interaction.delete_original_response.assert_awaited_once()
                    self.assertTrue(view.is_finished())
                    self.assertNotIn(key, self.service.views)
                else:
                    delay.assert_not_awaited()
                    interaction.response.edit_message.assert_awaited_once()
                    interaction.delete_original_response.assert_not_awaited()
            self.assertIsNotNone(self.service.store.active_effect(1, 2))
        self.assertIsNotNone(self.provisions.active_effect(1, 2))

    async def test_full_offer_deletion_failure_removes_buttons(self):
        _, offer = await self.service.buy_round(self.interaction, 'table')
        for user_id in range(2, offer['capacity'] + 1):
            self.service.store.claim(offer['id'], 1, user_id)
        self.store.create_player(1, 99)
        for error in (discord.NotFound, discord.Forbidden):
            with self.subTest(error=error):
                view = self.service.offer_view(offer['id'])
                interaction = SimpleNamespace(
                    guild_id=1, user=SimpleNamespace(id=99, bot=False),
                    response=SimpleNamespace(defer=AsyncMock()),
                    delete_original_response=AsyncMock(side_effect=error(
                        SimpleNamespace(status=404 if error is discord.NotFound else 403,
                                        reason='test'), 'test')),
                    edit_original_response=AsyncMock())
                with patch.object(self.service.store, 'claim_count', return_value=offer['capacity']), \
                        patch.object(self.service.store, 'claim', return_value=offer), \
                        patch('core.rpg_tavern.asyncio.sleep', new_callable=AsyncMock):
                    await view.claim_button.callback(interaction)
                self.assertTrue(view.is_finished())
                if error is discord.Forbidden:
                    self.assertIsNone(interaction.edit_original_response.call_args.kwargs['view'])
                else:
                    interaction.edit_original_response.assert_awaited_once()

    async def test_tavern_panel_shows_active_drink_and_meal(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'fishing:pond:common',3)")
            self.store.db.execute(
                "INSERT INTO rpg_inventory VALUES (1,1,'farming:potato',2)")
        _, meal = await self.service.serve_meal(
            self.interaction,
            ['fishing:pond:common'] * 3 + ['farming:potato'] * 2)
        _, offer = await self.service.buy_round(self.interaction, 'table')
        self.service.store.claim(offer['id'], 1, 1)
        view = TavernView(self.cog, self.interaction)
        self.addCleanup(view.stop)

        fields = {field.name: field.value for field in view.embed().fields}
        self.assertIn('討伐 XP +5%', fields['目前飲料效果'])
        self.assertIn(meal['data']['name'], fields['目前料理效果'])
        self.assertIn('剩餘', fields['目前料理效果'])
        self.assertIn('到期', fields['目前料理效果'])


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
