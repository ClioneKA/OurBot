from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

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
                                    defer=AsyncMock(), send_modal=AsyncMock()),
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
            SimpleNamespace(jump_url='https://discord.test/meal'),
            {'capacity': data['capacity'], 'data': data})
        await view.handle(self.interaction, 'cook')
        self.interaction.response.defer.assert_awaited_once()
        self.tavern.serve_meal.assert_awaited_once()
        embed = self.interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('料理 XP', embed.fields[-1].value)
        self.assertEqual(view.ingredients, [])

    async def test_d_grade_can_open_and_selected_ingredients_can_be_donated(self):
        self.grant('fishing:pond:common', 6)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = ['fishing:pond:common'] * 5
        view.rebuild()
        cook = next(child for child in view.children
                    if isinstance(child, discord.ui.Button) and child.label == '完成料理並開桌')
        self.assertFalse(cook.disabled)

        view.ingredients = ['fishing:pond:common']
        await view.handle(self.interaction, 'donate')
        self.assertEqual(self.provisions.state(1, 1)['xp'], 25)
        self.assertEqual(view.ingredients, [])
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('捐給監獄', embed.fields[-1].value)
        load = next(child for child in view.children
                    if isinstance(child, discord.ui.Button) and child.label == '載入上一份配方')
        self.assertFalse(load.disabled)
        await view.handle(self.interaction, 'repeat')
        self.assertEqual(view.ingredients, ['fishing:pond:common'])

    async def test_aftertaste_meal_keeps_public_seats(self):
        self.grant('fishing:waterway:rare', 4)
        self.grant('cooking:seasoning:high')
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = ['fishing:waterway:rare'] * 4 + ['cooking:seasoning:high']
        data = self.provisions.preview(view.ingredients, 1, 1)
        self.assertEqual((data['duration'], data['capacity']), (3, data['total_portions']))
        self.tavern.serve_meal.return_value = (
            SimpleNamespace(jump_url='https://discord.test/meal'),
            {'capacity': data['capacity'], 'data': data})

        view.rebuild()
        cook = next(child for child in view.children
                    if isinstance(child, discord.ui.Button) and child.label == '完成料理並開桌')
        self.assertFalse(cook.disabled)
        await view.handle(self.interaction, 'cook')

        embed = self.interaction.edit_original_response.call_args.kwargs['embed']
        self.assertIn('不同客人', embed.fields[-1].value)

    async def test_load_last_recipe_only_fills_selection(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 6)
        self.grant('farming:potato', 4)
        previous = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.publish(previous['id'], 99)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)

        repeat = next(child for child in view.children
                      if isinstance(child, discord.ui.Button) and child.label == '載入上一份配方')
        self.assertFalse(repeat.disabled)
        await view.handle(self.interaction, 'repeat')

        self.assertEqual(view.ingredients, ingredients)
        self.tavern.serve_meal.assert_not_awaited()
        self.interaction.response.defer.assert_not_awaited()
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('已載入上一份配方', embed.fields[-1].value)

    async def test_repeat_last_meal_is_disabled_when_ingredients_are_insufficient(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        previous = self.provisions.cook(1, 1, 9, ingredients, now=100)
        self.provisions.publish(previous['id'], 99)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)

        repeat = next(child for child in view.children
                      if isinstance(child, discord.ui.Button) and child.label == '載入上一份配方')
        self.assertTrue(repeat.disabled)

    async def test_recipe_presets_save_rename_select_load_and_clear(self):
        ingredients = ['fishing:pond:common'] * 3 + ['farming:potato'] * 2
        self.grant('fishing:pond:common', 3)
        self.grant('farming:potato', 2)
        view = ProvisionView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        view.ingredients = list(ingredients)
        view.rebuild()

        await view.handle(self.interaction, 'preset_save')
        self.assertEqual(self.provisions.preset(1, 1, 1)['ingredients'], ingredients)
        await view.handle(self.interaction, 'preset_rename_value', '成長宴席')
        self.assertEqual(view.current_preset()['name'], '成長宴席')

        await view.handle(self.interaction, 'reset')
        await view.handle(self.interaction, 'preset_load')
        self.assertEqual(view.ingredients, ingredients)
        self.assertIn('尚未消耗素材',
                      self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)

        await view.handle(self.interaction, 'preset_slot', '2')
        self.assertEqual(view.preset_slot, 2)
        self.assertTrue(next(child for child in view.children
                             if getattr(child, 'label', '') == '載入配方').disabled)
        await view.handle(self.interaction, 'preset_slot', '1')
        await view.handle(self.interaction, 'preset_clear')
        self.assertIsNone(view.current_preset()['ingredients'])
        self.assertLessEqual(len(view.to_components()), 5)


if __name__ == '__main__':
    unittest.main()
