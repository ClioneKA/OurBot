from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_farming import Farming
from core.rpg_farming_view import FarmingView
from core.settings import RPGSettings


class FarmingViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.farming = Farming(self.store)
        self.cog = SimpleNamespace(characters=self.characters, farming=self.farming,
                                   settings=settings, menu_views=WeakSet(), character_embed=lambda *args: None)
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = FarmingView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_plant_and_harvest_from_selected_plot(self):
        self.assertIn('農耕 Lv.**1**', self.view.embed().description)
        self.assertLessEqual(len(self.view.to_components()), 5)
        location_select = self.view.children[0]
        self.assertEqual([option.value for option in location_select.options], ['courtyard'])
        await self.view.handle(self.interaction, 'location', 'prison')
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('農耕 Lv.20', embed.fields[-1].value)
        await self.view.handle(self.interaction, 'notify')
        self.assertTrue(self.farming.state(1, 1)['notify'])
        with patch('core.rpg_farming.time.time', return_value=100), \
             patch('core.rpg_farming_view.time.time', return_value=100):
            await self.view.handle(self.interaction, 'plant')
        self.assertEqual(self.farming.state(1, 1)['sessions']['courtyard']['plant_id'], 'potato')
        with patch('core.rpg_farming.time.time', return_value=3700), \
             patch('core.rpg_farming_view.time.time', return_value=3700):
            await self.view.handle(self.interaction, 'harvest')
        self.assertEqual(self.characters.inventory_counts(1, 1)['farming:potato'], 2)
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('收成 馬鈴薯 ×2', embed.fields[-1].value)

    async def test_cancel_has_no_rewards(self):
        with patch('core.rpg_farming.time.time', return_value=100), \
             patch('core.rpg_farming_view.time.time', return_value=100):
            await self.view.handle(self.interaction, 'plant')
            await self.view.handle(self.interaction, 'cancel')
        state = self.farming.state(1, 1)
        self.assertEqual((state['sessions']['courtyard']['status'], state['xp']), ('cancelled', 0))
        self.assertNotIn('farming:potato', self.characters.inventory_counts(1, 1))
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertEqual(embed.fields[0].value, '目前閒置')
        self.assertIn('不會獲得作物或農耕 XP', embed.fields[-1].value)


if __name__ == '__main__':
    unittest.main()
