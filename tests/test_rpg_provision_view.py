from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_provision_view import ProvisionLoadoutView, ProvisionView
from core.rpg_provisions import Provisions
from core.settings import RPGSettings


class ProvisionViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.provisions = Provisions(self.store)
        self.cog = SimpleNamespace(characters=self.characters, provisions=self.provisions,
                                   settings=settings, menu_views=WeakSet(), character_embed=lambda *args: None)
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    def grant(self, key):
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_inventory VALUES (1,1,?,1)', (key,))

    async def test_crafting_panel_only_crafts_food(self):
        self.assertLessEqual(len(self.view.to_components()), 5)
        self.assertEqual(len(self.view.children), 6)
        self.grant('fishing:pond:common')
        self.grant('farming:potato')
        await self.view.handle(self.interaction, 'craft')
        self.assertEqual(self.characters.inventory_counts(1, 1)['food:pond:common'], 1)
        self.assertEqual(self.provisions.loadout(1, 1), {})
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('成功製作', embed.fields[-1].value)

    async def test_equipment_loadout_panel_selects_food(self):
        self.grant('food:pond:common')
        loadout_view = ProvisionLoadoutView(self.cog, self.interaction)
        self.addCleanup(loadout_view.stop)
        await loadout_view.handle(self.interaction, 'loadout_food', 'food:pond:common')
        self.assertEqual(self.provisions.loadout(1, 1)['food'], 'food:pond:common')
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('鯽魚馬鈴薯湯', embed.fields[0].value)
        self.assertLessEqual(len(loadout_view.to_components()), 5)

    async def test_switches_recipe_group(self):
        await self.view.handle(self.interaction, 'recipe_group', 'potion2')
        self.assertTrue(self.view.recipe_id.startswith('potion:2:'))
        self.assertIn('月光水草', self.view.embed().fields[0].value)


if __name__ == '__main__':
    unittest.main()
