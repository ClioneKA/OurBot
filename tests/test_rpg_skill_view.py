from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore
from core.rpg_battle import Tactics
from core.rpg_character import Characters
from core.rpg_skill_view import SkillView
from core.settings import RPGSettings


class SkillViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.tactics = Tactics(self.store)
        cog = SimpleNamespace(characters=self.characters, tactics=self.tactics,
                              skills_embed=lambda *args: discord.Embed(title='技能'))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = SkillView(cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_controls_save_immediately_and_preserve_other_panel_changes(self):
        await self.view.handle(self.interaction, 'slot', '2')
        await self.view.handle(self.interaction, 'priority', '1')
        await self.view.handle(self.interaction, 'condition', 'self40')
        await self.view.handle(self.interaction, 'target', 'self')
        await self.view.handle(self.interaction, 'toggle')
        rule = self.view.current()
        self.assertEqual((rule.priority, rule.condition, rule.target, rule.enabled), (1, 'self40', 'self', False))
        self.tactics.configure(1, 1, '民兵', 2, 2, False, 'ally50', 'lowest')
        await self.view.handle(self.interaction, 'toggle')
        rule = self.view.current()
        self.assertEqual((rule.priority, rule.condition, rule.target, rule.enabled), (2, 'ally50', 'lowest', True))
        self.assertEqual(len(self.view.to_components()), 5)

    async def test_job_change_fixed_target_and_attack_restrictions(self):
        self.store.award_voice([(1, 1, 1154)])
        self.characters.change_job(1, 1, '騎士')
        await self.view.handle(self.interaction, 'toggle')
        self.assertEqual(self.view.job, '騎士')
        self.assertTrue(self.view.current().enabled)
        await self.view.handle(self.interaction, 'slot', '2')
        self.assertTrue(self.view.children[3].disabled)
        self.assertIn('全隊', self.view.children[3].options[0].label)
        await self.view.handle(self.interaction, 'target', 'self')
        self.assertEqual(self.view.current().target, 'lowest')
        self.characters.change_job(1, 1, '弓兵')
        await self.view.handle(self.interaction, 'refresh')
        self.assertNotIn('self', [o.value for o in self.view.children[3].options])
        await self.view.handle(self.interaction, 'target', 'self')
        self.assertEqual(self.view.current().target, 'lowest')

    async def test_unauthorized_and_expired_interactions_do_not_write(self):
        other = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
                                response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(other, 'toggle')
        other.response.send_message.assert_awaited_once()
        self.assertTrue(self.view.current().enabled)
        await self.view.on_timeout()
        self.interaction.edit_original_response.assert_awaited_once()
        await self.view.handle(self.interaction, 'toggle')
        self.assertTrue(self.view.current().enabled)
        self.assertTrue(self.view.is_finished())
