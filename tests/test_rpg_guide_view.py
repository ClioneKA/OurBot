from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg_guide_view import CATEGORIES, RaidGuideView, guide_embed
from core.rpg_monsters import MONSTER_GUIDES
from core.rpg_raids import HIGH_RAID_KINDS, MID_KINDS, REGULAR_KINDS, SPECIAL_KIND


class RaidGuideViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.view = RaidGuideView(self.interaction)
        self.addCleanup(self.view.stop)

    def test_every_scheduled_monster_has_a_guide(self):
        expected = set(REGULAR_KINDS + MID_KINDS + HIGH_RAID_KINDS + (SPECIAL_KIND,))
        self.assertTrue(expected <= MONSTER_GUIDES.keys())

    def test_all_pages_fit_discord_limits(self):
        for category, (_, entries) in CATEGORIES.items():
            for entry in ('overview', *entries):
                embed = guide_embed(category, entry)
                self.assertLessEqual(len(embed), 6000)
                self.assertLessEqual(len(embed.description or ''), 4096)
                self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_category_and_entry_switch_in_place(self):
        await self.view.handle(self.interaction, 'category', 'high')
        self.assertEqual((self.view.category, self.view.entry), ('high', 'overview'))
        await self.view.handle(self.interaction, 'entry', '星蝕巨神')
        self.assertIn('星蝕巨神', self.view.embed().title)
        self.assertIn('星核', self.view.embed().description)
        self.assertLessEqual(len(self.view.to_components()), 5)

    async def test_foreign_user_cannot_control_panel(self):
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(
            edit_message=AsyncMock(), send_message=AsyncMock()))
        await self.view.handle(stranger, 'category', 'witch')
        self.assertEqual(self.view.category, 'regular')
        stranger.response.send_message.assert_awaited_once()

