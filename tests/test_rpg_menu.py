from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord
from core.rpg import RPGStore
from core.rpg_character import Characters, JOBS
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
        self.cog = SimpleNamespace(characters=self.characters, settings=settings, menu_views=WeakSet(),
                                   character_embed=lambda *args: discord.Embed(title='角色'))
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
