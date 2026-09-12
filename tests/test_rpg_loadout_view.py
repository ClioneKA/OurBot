from pathlib import Path
import json
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics
from core.rpg_character import Characters
from core.rpg_loadouts import Loadouts
from core.rpg_loadout_view import LoadoutView
from core.rpg_menu import AdventureView
from core.settings import RPGSettings


class LoadoutViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        characters = Characters(self.store, settings)
        tactics = Tactics(self.store)
        loadouts = Loadouts(self.store, characters, tactics)
        self.store.award_voice([(1, 1, level_floor(50))])
        characters.change_job(1, 1, '僧侶')
        self.cog = SimpleNamespace(characters=characters, tactics=tactics, loadouts=loadouts,
                                   settings=settings, menu_views=WeakSet(),
                                   character_embed=lambda *args: discord.Embed(title='角色'))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                     send_modal=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = LoadoutView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_select_and_save_purchased_slot(self):
        self.cog.characters.grant_item(1, 1, 'proof:raid', 20)
        self.cog.characters.expansions.buy(1, 1, 'expansion:loadout', expected_purchased=0)
        await self.view.handle(self.interaction, 'refresh')
        await self.view.handle(self.interaction, 'slot', '4')
        await self.view.handle(self.interaction, 'save')
        self.assertEqual(self.view.current()['slot'], 4)
        self.assertEqual(self.view.current()['data']['job'], '僧侶')

    async def test_save_select_rename_apply_and_clear(self):
        self.assertTrue(next(child for child in self.view.children
                             if getattr(child, 'label', '') == '套用配置').disabled)
        await self.view.handle(self.interaction, 'save')
        self.assertIn('已將目前', self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
        await self.view.handle(self.interaction, 'rename_value', '治療配置')
        self.assertEqual(self.view.current()['name'], '治療配置')
        self.cog.characters.change_job(1, 1, '弓兵')
        await self.view.handle(self.interaction, 'apply')
        self.assertEqual(self.cog.characters.job(1, 1), '僧侶')
        await self.view.handle(self.interaction, 'clear')
        self.assertIsNone(self.view.current()['data'])
        self.assertLessEqual(len(self.view.to_components()), 5)

    async def test_apply_missing_equipment_reports_skipped_slots(self):
        await self.view.handle(self.interaction, 'save')
        saved = self.view.current()
        weapon_id = saved['data']['equipment']['武器']
        self.cog.characters.change_job(1, 1, '弓兵')
        with self.store.db:
            self.store.db.execute('DELETE FROM rpg_equipment_instances WHERE instance_id=?', (weapon_id,))
        await self.view.handle(self.interaction, 'apply')
        embed = self.interaction.response.edit_message.call_args.kwargs['embed']
        self.assertIn('已套用', embed.fields[-1].value)
        self.assertIn('已略過遺失的裝備：武器', embed.fields[-1].value)
        self.assertIn('物品已遺失', embed.description)
        state = self.cog.characters.snapshot(1, 1)
        self.assertEqual(state['job'], '僧侶')
        self.assertNotIn('武器', state['equipped_instances'])
        self.assertEqual(state['equipped_instances'], {
            slot: value for slot, value in saved['data']['equipment'].items() if slot != '武器'})

    async def test_equipment_is_displayed_in_slot_order(self):
        charm_id = self.cog.characters.grant_item(1, 1, 'puppet:twin_charm')[0]
        self.cog.characters.equip(1, 1, charm_id, 2)
        await self.view.handle(self.interaction, 'save')
        profile = self.view.current()
        equipment = profile['data']['equipment']
        profile['data']['equipment'] = {
            '飾品2': equipment['飾品2'],
            '套裝': equipment['套裝'],
            '武器': equipment['武器'],
        }
        with self.store.db:
            self.store.db.execute(
                'UPDATE rpg_loadouts SET data=? WHERE guild_id=1 AND user_id=1 AND slot=1',
                (json.dumps(profile['data'], ensure_ascii=False),))

        description = self.view.embed().description
        self.assertLess(description.index('武器：'), description.index('套裝：'))
        self.assertLess(description.index('套裝：'), description.index('飾品2：'))

    async def test_main_menu_navigates_to_loadouts(self):
        menu = AdventureView(self.cog, self.interaction)
        self.addCleanup(menu.stop)
        labels = [child.label for child in menu.children if isinstance(child, discord.ui.Button)]
        self.assertIn('出戰配置', labels)
        await menu.handle(self.interaction, 'loadouts')
        panel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(panel.stop)
        self.assertIsInstance(panel, LoadoutView)
