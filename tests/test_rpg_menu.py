from pathlib import Path
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
        self.cog = SimpleNamespace(characters=self.characters, settings=settings, menu_views=WeakSet(),
                                   divinations=self.divinations,
                                   character_embed=lambda *args: discord.Embed(title='角色'),
                                   adventurer_embed=lambda *args: discord.Embed(title='冒險者名片'))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = AdventureView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

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
        for key in ('paint:red', 'paint:yellow', 'paint:blue', 'noah:unfinished'):
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
        self.assertEqual(panel.catalog, ['recipe:paint_set', 'noah:unfinished'])
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['paint:set'], 1)
        await panel.handle(self.interaction, 'select', 'noah:unfinished')
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['noah:unfinished'], 1)
        notice = self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value
        self.assertIn('尚未開放', notice)
        self.assertIn('沒有消耗', notice)

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
        self.assertIn('寶生瑪格的占卜室', room.embed().title)
        self.assertIn('300 金幣', room.embed().description)
