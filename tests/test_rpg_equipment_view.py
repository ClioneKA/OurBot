from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore, level_floor
from core.rpg_character import Characters
from core.rpg_equipment_view import EquipmentView
from core.settings import RPGSettings


class EquipmentViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.cog = SimpleNamespace(characters=self.characters, settings=settings,
                                   character_embed=lambda *args: discord.Embed(title='角色'))
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = EquipmentView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_starter_inventory_and_unauthorized_user(self):
        self.assertFalse(self.view.children[1].disabled)
        self.assertTrue(self.view.wear.disabled)
        self.assertFalse(self.view.remove.disabled)
        self.assertEqual(self.view.provisions.label, '討伐補給')
        self.assertLessEqual(len(self.view.to_components()), 5)
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), guild_id=1,
                                   response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(stranger, 'close')
        self.assertFalse(self.view.closed)
        stranger.response.send_message.assert_awaited_once()

    async def test_job_change_invalidates_selection_and_locked_slots(self):
        self.store.award_voice([(1, 1, level_floor(10))])
        self.characters.change_job(1, 1, '騎士')
        await self.view.handle(self.interaction, 'item', '騎士:0:武器')
        self.characters.change_job(1, 1, '弓兵')
        await self.view.handle(self.interaction, 'wear')
        self.assertEqual(self.characters.snapshot(1, 1)['equipped']['武器'], '弓兵:0:武器')
        self.assertIsNone(self.view.item_id)
        await self.view.handle(self.interaction, 'slot', '飾品4')
        self.assertEqual(self.view.slot, '武器')
        self.assertIn('尚未解鎖', self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)

    async def test_timeout_disables_actions_and_refresh_unlocks_slots(self):
        self.store.award_voice([(1, 1, level_floor(80))])
        self.characters.change_job(1, 1, '僧侶')
        await self.view.handle(self.interaction, 'refresh')
        self.assertEqual(len(self.view.children[0].options), 7)
        self.assertLessEqual(len(self.view.children[1].options), 25)
        await self.view.on_timeout()
        self.interaction.edit_original_response.assert_awaited_once()
        self.assertTrue(self.view.closed)
        await self.view.handle(self.interaction, 'remove')
        self.assertIn('面板已關閉', self.interaction.response.send_message.call_args.args[0])
        self.assertIn('武器', self.characters.snapshot(1, 1)['equipped'])
