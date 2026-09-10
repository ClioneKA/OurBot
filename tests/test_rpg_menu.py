from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord
from core.rpg import RPGStore
from core.rpg_character import Characters, JOBS
from core.rpg_divination import Divinations
from core.rpg_menu import AdventureView
from core.rpg_help import HELP_TOPICS
from core.rpg_painted_maze_rewards import PaintedMazeRewardStore
from core.settings import RPGSettings


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.divinations = Divinations(self.store)
        self.rewards = PaintedMazeRewardStore(self.store)
        self.cog = SimpleNamespace(characters=self.characters, settings=settings, menu_views=WeakSet(),
                                   divinations=self.divinations,
                                   painted_maze=SimpleNamespace(
                                       create=AsyncMock(return_value={'number': 7}),
                                       rewards=self.rewards),
                                   character_embed=lambda *args: discord.Embed(title='角色'),
                                   adventurer_embed=lambda *args: discord.Embed(title='冒險者名片'))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                     defer=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = AdventureView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_help_topics_switch_in_place_and_refresh_keeps_selection(self):
        await self.view.handle(self.interaction, 'help')
        guide = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(guide.stop)
        self.assertIn('新手入門', guide.embed().title)
        self.assertNotIn('三次方根', guide.embed().description)
        for topic, (label, _) in HELP_TOPICS.items():
            select = next(child for child in guide.children if isinstance(child, discord.ui.Select))
            select._values = [topic]
            await select.callback(self.interaction)
            self.assertIs(self.interaction.response.edit_message.call_args.kwargs['view'], guide)
            embed = guide.embed()
            self.assertIn(label, embed.title)
            self.assertLessEqual(len(embed.description), 4096)
            self.assertLessEqual(len(embed), 6000)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
            self.assertLessEqual(len(guide.to_components()), 5)
            await guide.handle(self.interaction, 'refresh')
            select = next(child for child in guide.children if isinstance(child, discord.ui.Select))
            self.assertEqual([option.value for option in select.options if option.default], [topic])
        await guide.handle(self.interaction, 'help_topic', 'unknown')
        self.assertEqual(guide.help_topic, 'advanced')

    async def test_help_topic_rejects_foreign_user_and_closed_panel(self):
        guide = AdventureView(self.cog, self.interaction, 'help')
        self.addCleanup(guide.stop)
        stranger = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        await guide.handle(stranger, 'help_topic', 'advanced')
        self.assertEqual(guide.help_topic, 'intro')
        stranger.response.edit_message.assert_not_awaited()
        await guide.handle(self.interaction, 'close')
        await guide.handle(self.interaction, 'help_topic', 'advanced')
        self.assertEqual(guide.help_topic, 'intro')

    async def test_paused_xp_notice_is_visible_on_intro_and_growth(self):
        self.cog.settings = replace(self.cog.settings, enabled=False)
        guide = AdventureView(self.cog, self.interaction, 'help')
        self.addCleanup(guide.stop)
        for topic in ('intro', 'growth'):
            await guide.handle(self.interaction, 'help_topic', topic)
            self.assertTrue(any('暫停聊天與語音經驗' in field.value for field in guide.embed().fields))

    async def test_stale_view_and_timeout_cannot_overwrite_new_page(self):
        await self.view.handle(self.interaction, 'help')
        child = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(child.stop)
        self.interaction.response.edit_message.reset_mock()
        await self.view.on_timeout()
        self.interaction.edit_original_response.assert_not_awaited()
        await self.view.handle(self.interaction, 'jobs')
        self.interaction.response.edit_message.assert_not_awaited()
        await child.on_timeout()
        self.interaction.edit_original_response.assert_awaited_once()
        await child.handle(self.interaction, 'home')
        self.interaction.response.edit_message.assert_not_awaited()

    async def test_foreign_user_and_guild_cannot_navigate_or_change_jobs(self):
        for guild, user in ((1, 2), (2, 1)):
            stranger = SimpleNamespace(guild_id=guild, user=SimpleNamespace(id=user),
                response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
            await self.view.handle(stranger, 'jobs')
            stranger.response.edit_message.assert_not_awaited()
            stranger.response.send_message.assert_awaited_once()
        self.assertFalse(self.view.closed)

    async def test_backpack_pagination_bounds_and_component_rows(self):
        self.store.award_voice([(1, 1, 200000000)])
        for job in JOBS:
            self.characters.change_job(1, 1, job)
        await self.view.handle(self.interaction, 'backpack')
        bag = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(bag.stop)
        self.assertIn('背包 1/2', bag.embed().title)
        await bag.handle(self.interaction, 'next')
        self.assertIn('背包 2/2', bag.embed().title)
        await bag.handle(self.interaction, 'next')
        self.assertEqual(bag.index, 1)
        await bag.handle(self.interaction, 'previous')
        await bag.handle(self.interaction, 'previous')
        self.assertEqual(bag.index, 0)
        self.assertLessEqual(len(bag.to_components()), 5)

    async def test_backpack_uses_single_extensible_item_action_panel(self):
        for key in ('paint:red', 'paint:yellow', 'paint:blue', 'noah:unfinished',
                    'painting:balloon'):
            self.characters.grant_item(1, 1, key)
        await self.view.handle(self.interaction, 'backpack')
        bag = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(bag.stop)
        labels = [child.label for child in bag.children if isinstance(child, discord.ui.Button)]
        self.assertIn('使用道具', labels)
        self.assertNotIn('組合噴漆罐', labels)
        self.assertNotIn('使用噴漆罐套組', labels)

        self.interaction.response.edit_message.reset_mock()
        await bag.handle(self.interaction, 'use_items')
        panel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(panel.stop)
        self.assertEqual(panel.catalog,
                         ['recipe:paint_set', 'noah:unfinished', 'painting:balloon'])
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['paint:set'], 1)
        await panel.handle(self.interaction, 'select', 'noah:unfinished')
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['noah:unfinished'], 1)
        self.cog.painted_maze.create.assert_awaited_once_with(
            self.interaction, 'noah:unfinished')
        notice = self.interaction.edit_original_response.call_args.kwargs['embed'].fields[-1].value
        self.assertIn('繪境迷宮 #7', notice)
        self.assertIn('開始探索時消耗', notice)

        self.characters.grant_item(1, 1, 'maze:choice_box:archer')
        panel.rebuild()
        await panel.handle(self.interaction, 'select', 'maze:choice_box:archer')
        await panel.handle(self.interaction, 'choose:weapon')
        self.assertEqual(self.characters.inventory_counts(1, 1).get(
            'maze:choice_box:archer', 0), 0)
        self.assertTrue(any(entry.item_id == 'maze:archer:weapon'
                            for entry in self.characters.inventory_entries(1, 1)))

    async def test_profile_page_sets_and_clears_showcase(self):
        self.characters.grant_item(1, 1, 'paint:red')
        await self.view.handle(self.interaction, 'profile')
        profile = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(profile.stop)
        await profile.handle(self.interaction, 'showcase', 'paint:red')
        self.assertEqual(self.characters.showcase(1, 1), 'paint:red')
        self.assertIn('現在展示', self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
        await profile.handle(self.interaction, 'clear')
        self.assertIsNone(self.characters.showcase(1, 1))

    async def test_movement_page_opens_mag_divination_room(self):
        labels = [child.label for child in self.view.children if isinstance(child, discord.ui.Button)]
        self.assertIn('移動', labels)
        await self.view.handle(self.interaction, 'travel')
        travel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(travel.stop)
        self.assertIn('移動', travel.embed().title)
        await travel.handle(self.interaction, 'divination')
        room = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(room.stop)
        self.assertIn('瑪格的占卜室', room.embed().title)
        self.assertIn('300 金幣', room.embed().description)
