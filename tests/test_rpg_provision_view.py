from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_provision_view import ProvisionView
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
        self.tavern = SimpleNamespace(serve_meal=AsyncMock())
        self.cog = SimpleNamespace(characters=self.characters, provisions=self.provisions,
                                   tavern=self.tavern, settings=settings, menu_views=WeakSet(),
                                   character_embed=lambda *args: None)
        self.interaction = SimpleNamespace(
            guild_id=1, channel_id=9, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                    defer=AsyncMock()),
            edit_original_response=AsyncMock())

    def grant(self, key, quantity=1):
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_inventory VALUES (1,1,?,?)', (key, quantity))

    async def test_panel_selects_five_owned_ingredients_and_previews(self):
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        for key in ['fishing:pond:common'] * 3 + ['farming:potato'] * 2:
            await view.handle(self.interaction, 'ingredient', key)
        self.assertEqual(len(view.ingredients), 5)
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('料理預覽', [field.name for field in embed.fields])
        self.assertIn('成長', embed.fields[-1].value)
        self.assertLessEqual(len(view.to_components()), 5)

    async def test_completed_meal_is_published_by_tavern(self):
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        data = self.provisions.preview(view.ingredients, 1, 1)
        self.tavern.serve_meal.return_value = (
            SimpleNamespace(jump_url='https://discord.test/meal'), {'data': data})
        await view.handle(self.interaction, 'cook')
        self.interaction.response.defer.assert_awaited_once()
        self.tavern.serve_meal.assert_awaited_once()
        embed = self.interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('料理 XP', embed.fields[-1].value)
        self.assertEqual(view.ingredients, [])

    async def test_c_grade_can_open_and_selected_ingredients_can_be_donated(self):
        self.grant('fishing:pond:common', 6)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = ['fishing:pond:common'] * 5
        view.rebuild()
        cook = next(child for child in view.children if child.label == '完成料理並開桌')
        self.assertFalse(cook.disabled)

        view.ingredients = ['fishing:pond:common']
        await view.handle(self.interaction, 'donate')
        self.assertEqual(self.provisions.state(1, 1)['xp'], 25)
        self.assertEqual(view.ingredients, [])
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('捐給監獄', embed.fields[-1].value)

    async def test_one_seat_meal_is_shown_and_completed_as_private(self):
        self.grant('fishing:waterway:rare', 4)
        self.grant('farming:moonwhite_rice')
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = ['fishing:waterway:rare'] * 4 + ['farming:moonwhite_rice']
        data = self.provisions.preview(view.ingredients, 1, 1)
        self.assertEqual(data['capacity'], 1)
        self.tavern.serve_meal.return_value = (None, {'capacity': 1, 'data': data})

        view.rebuild()
        cook = next(child for child in view.children if child.label == '完成私人料理')
        self.assertFalse(cook.disabled)
        await view.handle(self.interaction, 'cook')

        embed = self.interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('不發布酒館公告', embed.fields[-1].value)


if __name__ == '__main__':
    unittest.main()
