from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore, level_floor
from core.rpg_character import Characters, DYE_PRICE, EMBROIDERY_PRICE, ITEMS
from core.rpg_crystal_view import CrystalTailorView
from core.rpg_crystals import CRYSTAL_REMOVAL_PRICE, CrystalStore
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

    async def test_crystal_services_are_available_inside_tailor_shop(self):
        outline_id = self.add_crystal('outline')
        color_id = self.add_crystal('color', 1)
        elite_id = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]
        view = CrystalTailorView(self.cog, self.interaction)
        self.addCleanup(view.stop)
        labels = [child.label for child in view.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(labels[:5], ['結晶一覽', '鑲嵌', '拆除', '出售', '給予'])

        await view.handle(self.interaction, 'mode:socket')
        await view.handle(self.interaction, 'crystal', str(outline_id))
        await view.handle(self.interaction, 'equipment', f'instance:{elite_id}')
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(outline_id).equipment_instance_id, elite_id)

        await view.handle(self.interaction, 'mode:remove')
        await view.handle(self.interaction, 'equipment', f'instance:{elite_id}')
        await view.handle(self.interaction, 'slot', 'outline')
        await view.handle(self.interaction, 'apply')
        self.assertIsNone(self.crystals.get(outline_id).equipment_instance_id)
        self.assertEqual(self.store.gold(1, 1), 5000 - CRYSTAL_REMOVAL_PRICE)

        await view.handle(self.interaction, 'mode:sell')
        await view.handle(self.interaction, 'crystal', str(outline_id))
        await view.handle(self.interaction, 'apply')
        self.assertIsNone(self.crystals.get(outline_id))

        recipient = SimpleNamespace(id=2, bot=False, mention='<@2>')
        await view.handle(self.interaction, 'mode:give')
        await view.handle(self.interaction, 'crystal', str(color_id))
        await view.handle(self.interaction, 'recipient', recipient)
        await view.handle(self.interaction, 'apply')
        self.assertEqual(self.crystals.get(color_id).user_id, 2)


if __name__ == '__main__':
    unittest.main()
