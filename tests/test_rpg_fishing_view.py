from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_fishing import Fishing
from core.rpg_fishing_view import FishingView
from core.settings import RPGSettings


class FixedRandom:
    def __init__(self, value=0.99):
        self.value = value

    def random(self):
        return self.value


class FishingViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.fishing = Fishing(self.store, FixedRandom())
        self.cog = SimpleNamespace(characters=self.characters, fishing=self.fishing,
                                   settings=settings, store=self.store, menu_views=WeakSet(),
                                   character_embed=lambda *args: None)
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = FishingView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_start_wait_claim_and_notification_toggle(self):
        self.assertIn('釣魚 Lv.**1**', self.view.embed().description)
        self.assertLessEqual(len(self.view.to_components()), 5)
        with patch('core.rpg_fishing.time.time', return_value=100), \
             patch('core.rpg_fishing_view.time.time', return_value=100):
            await self.view.handle(self.interaction, 'start')
        state = self.fishing.state(1, 1)
        self.assertEqual((state['session']['spot_id'], state['session']['base_catches']), ('pond', 2))
        with patch('core.rpg_fishing.time.time', return_value=1900), \
             patch('core.rpg_fishing_view.time.time', return_value=1900):
            await self.view.handle(self.interaction, 'claim')
        self.assertEqual(self.fishing.state(1, 1)['xp'], 200)
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('共捕獲 2 次', embed.fields[-1].value)
        await self.view.handle(self.interaction, 'notify')
        self.assertTrue(self.fishing.state(1, 1)['notify'])

    async def test_level_gate_crafting_and_foreign_user(self):
        await self.view.handle(self.interaction, 'spot', 'lake')
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('Lv.20', embed.fields[-1].value)
        with self.store.db:
            for key in ('fishing:pond:rod', 'fishing:pond:line', 'fishing:pond:hook'):
                self.store.db.execute('INSERT INTO rpg_inventory VALUES (1,1,?,1)', (key,))
        await self.view.handle(self.interaction, 'craft')
        self.assertEqual(self.fishing.state(1, 1)['rod_id'], 'fishing:rod:simple')
        stranger = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
                                   response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(stranger, 'start')
        stranger.response.send_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
