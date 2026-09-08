from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore, level_floor
from core.rpg_character import Characters, DYE_PRICE, EMBROIDERY_PRICE, ITEMS
from core.rpg_crystal_view import CrystalTailorView
from core.rpg_crystals import CrystalStore
from core.rpg_tailor_view import TailorView
from core.settings import RPGSettings


class TailorViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.crystals = CrystalStore(self.store)
        self.characters.create(1, 2)
        self.store.award_voice([(1, 1, level_floor(45))])
        self.characters.change_job(1, 1, '弓兵')
        self.noah_id = self.characters.grant_item(1, 1, 'noah:archer:weapon')[0]
        self.raid_id = self.characters.grant_item(1, 1, 'raid:0')[0]
        self.characters.grant_item(1, 1, 'paint:blue')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,5000)')
        self.cog = SimpleNamespace(
            store=self.store, characters=self.characters, settings=self.settings,
            painted_maze=SimpleNamespace(crystals=self.crystals), menu_views=set())
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = TailorView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_dye_and_embroidery_are_paid_instance_operations(self):
        noah = f'instance:{self.noah_id}'
        await self.view.handle(self.interaction, 'item', noah)
        await self.view.handle(self.interaction, 'option', 'blue')
        await self.view.handle(self.interaction, 'apply')
        instance = self.characters.get_instance(1, 1, noah)
        self.assertEqual(self.characters.instance_item_id(instance), 'noah:archer:weapon:blue')
        self.assertEqual(self.store.gold(1, 1), 5000 - DYE_PRICE)

        await self.view.handle(self.interaction, 'mode:embroidery')
        self.assertFalse(any(entry.item_id.startswith('accessory:') for entry in self.view.entries.values()))
        raid = f'instance:{self.raid_id}'
        await self.view.handle(self.interaction, 'item', raid)
        await self.view.handle(self.interaction, 'option', 'wing')
        await self.view.handle(self.interaction, 'apply')
        item = self.characters.resolved_item(self.characters.get_instance(1, 1, raid))
        self.assertEqual(item.stats[3], ITEMS['raid:0'].stats[3] + 2)
        self.assertEqual(self.store.gold(1, 1), 5000 - DYE_PRICE - EMBROIDERY_PRICE)
        self.assertEqual(self.view.current_embroidery(raid), '羽翼刺繡')

    async def test_other_users_cannot_operate_panel(self):
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), guild_id=1,
                                   response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(stranger, 'close')
        self.assertFalse(self.view.closed)
        stranger.response.send_message.assert_awaited_once()

    def add_crystal(self, crystal_type, reward_slot=0):
        definitions = {
            'outline': ('outline_attack', '["攻擊"]', '[15]', None),
            'color': ('color_speed', '["speed"]', '[5]', None),
            'source': ('source_endless_arrow', '["endless_arrow"]', '[2]', '弓兵'),
        }
        affix, effects, values, job = definitions[crystal_type]
        with self.store.db:
            cursor = self.store.db.execute('''INSERT INTO rpg_crystal_instances
                (guild_id,user_id,crystal_type,quality,affix_id,effect_keys,rolled_values,
                 job,source_painting_id,source_room_id,source_user_id,source_stage,
                 reward_slot,created_at) VALUES (1,1,?,'習作',?,?,?,?,
                 'test','tailor-view',1,1,?,1)''',
                (crystal_type, affix, effects, values, job, reward_slot))
        return cursor.lastrowid

    async def test_equipment_first_socket_and_replace(self):
        first = self.add_crystal('outline')
        second = self.add_crystal('outline', 1)
        elite = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]
        reference = f'instance:{elite}'
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        self.assertEqual(view.mode, 'socket')
        self.assertEqual(view.available_crystals, set())
        self.assertNotIn('拆除', [getattr(child, 'label', '') for child in view.children])
        await view.handle(self.interaction, 'equipment', reference)
        self.assertEqual(view.available_crystals, {first, second})
        self.assertIn('空槽', str(view.embed().to_dict()))
        await view.handle(self.interaction, 'crystal', str(first))
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(first).equipment_instance_id, elite)
        self.assertEqual(view.selected_equipment, reference)
        await view.handle(self.interaction, 'crystal', str(second))
        self.assertIn('替換預覽', str(view.embed().to_dict()))
        self.assertIn('確認替換（摧毀舊結晶）', [getattr(child, 'label', '') for child in view.children])
        await view.handle(self.interaction, 'apply')
        self.assertIsNone(self.crystals.get(first))
        self.assertEqual(self.crystals.get(second).equipment_instance_id, elite)
        self.assertEqual(self.store.gold(1, 1), 5000)

    async def test_inventory_sell_and_give(self):
        first = self.add_crystal('outline')
        second = self.add_crystal('color', 1)
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'mode:inventory')
        await view.handle(self.interaction, 'crystal', str(first))
        await view.handle(self.interaction, 'mode:sell')
        self.assertEqual(view.selected_crystal, first)
        await view.handle(self.interaction, 'apply')
        self.assertIsNone(self.crystals.get(first))
        await view.handle(self.interaction, 'mode:give')
        await view.handle(self.interaction, 'crystal', str(second))
        await view.handle(self.interaction, 'recipient', SimpleNamespace(id=2, bot=False, mention='<@2>'))
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(second).user_id, 2)

    async def test_pagination_and_equipment_change(self):
        ids = {self.add_crystal('outline', index) for index in range(21)}
        for _ in range(26):
            self.characters.grant_item(1, 1, 'maze:archer:weapon')
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'equipment_next')
        select = next(child for child in view.children if getattr(child, 'action', None) == 'equipment')
        reference = select.options[-1].value
        await view.handle(self.interaction, 'equipment', reference)
        await view.handle(self.interaction, 'next')
        select = next(child for child in view.children if getattr(child, 'action', None) == 'crystal')
        crystal_id = int(select.options[0].value)
        self.assertIn(crystal_id, ids)
        self.assertEqual(view.selected_equipment, reference)
        await view.handle(self.interaction, 'crystal', str(crystal_id))
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(crystal_id).equipment_instance_id, view.equipment[reference].instance_id)
        await view.handle(self.interaction, 'equipment_previous')
        self.assertIsNone(view.selected_equipment)
        self.assertFalse(view.available_crystals)
        # Every row stays within Discord's component width limit.
        for row in range(5):
            self.assertLessEqual(sum(child.width for child in view.children if child.row == row), 5)

    async def test_crystal_filter_follows_equipment_job_and_socket_state(self):
        source = self.add_crystal('source')
        outline = self.add_crystal('outline', 1)
        archer = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]
        knight = self.characters.grant_item(1, 1, 'maze:knight:weapon')[0]
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'equipment', f'instance:{archer}')
        self.assertEqual(view.available_crystals, {source, outline})
        await view.handle(self.interaction, 'crystal', str(source))
        await view.handle(self.interaction, 'equipment', f'instance:{knight}')
        self.assertIsNone(view.selected_crystal)
        self.assertEqual(view.available_crystals, {outline})
        self.crystals.socket(1, 1, outline, f'instance:{knight}')
        await view.handle(self.interaction, 'refresh')
        self.assertFalse(view.available_crystals)
        self.assertIn('力量輪廓', str(view.embed().to_dict()))

    async def test_stale_slot_requires_updated_preview(self):
        first = self.add_crystal('outline')
        second = self.add_crystal('outline', 1)
        elite = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]
        reference = f'instance:{elite}'
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'equipment', reference)
        await view.handle(self.interaction, 'crystal', str(second))
        self.crystals.socket(1, 1, first, reference)
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(first).equipment_instance_id, elite)
        self.assertIsNone(self.crystals.get(second).equipment_instance_id)
        self.assertIn('替換預覽', str(view.embed().to_dict()))
        await view.handle(self.interaction, 'apply')
        self.assertIsNone(self.crystals.get(first))
        self.assertEqual(self.crystals.get(second).equipment_instance_id, elite)


if __name__ == '__main__':
    unittest.main()
