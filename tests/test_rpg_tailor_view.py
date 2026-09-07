from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore, level_floor
from core.rpg_character import Characters, DYE_PRICE, EMBROIDERY_PRICE, ITEMS
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
        self.store.award_voice([(1, 1, level_floor(45))])
        self.characters.change_job(1, 1, '弓兵')
        self.noah_id = self.characters.grant_item(1, 1, 'noah:archer:weapon')[0]
        self.raid_id = self.characters.grant_item(1, 1, 'raid:0')[0]
        self.characters.grant_item(1, 1, 'paint:blue')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,5000)')
        self.cog = SimpleNamespace(store=self.store, characters=self.characters, settings=self.settings)
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


if __name__ == '__main__':
    unittest.main()
