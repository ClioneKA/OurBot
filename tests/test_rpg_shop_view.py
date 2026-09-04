from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_shop_view import ShopView
from core.settings import RPGSettings


class ShopViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        # Exercise automatic migration of the original three-column inventory.
        self.store.db.execute('CREATE TABLE rpg_inventory (guild_id INTEGER, user_id INTEGER, item_id TEXT, PRIMARY KEY(guild_id,user_id,item_id))')
        self.store.db.execute("INSERT INTO rpg_inventory VALUES (1,1,'raid:0')")
        self.store.db.commit()
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 1)
        self.store.award_voice([(1, 1, 4470)])
        self.characters.change_job(1, 1, '騎士')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,1000)')
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = ShopView(SimpleNamespace(characters=self.characters, store=self.store, settings=settings), self.interaction)
        self.addCleanup(self.view.stop)

    async def test_purchase_and_repeated_click_charge_once(self):
        await self.view.handle(self.interaction, 'item', '騎士:1:武器')
        self.assertFalse(self.view.buy_button.disabled)
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.store.gold(1, 1), 500)
        self.assertTrue(self.view.buy_button.disabled)
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.store.gold(1, 1), 500)
        self.assertEqual(self.characters.inventory_counts(1, 1)['騎士:1:武器'], 1)

    async def test_stale_job_unauthorized_and_expired_clicks(self):
        await self.view.handle(self.interaction, 'item', '騎士:1:武器')
        other = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
                                response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(other, 'buy')
        other.response.send_message.assert_awaited_once()
        self.characters.change_job(1, 1, '弓兵')
        await self.view.handle(self.interaction, 'buy')
        self.assertIsNone(self.view.item_id)
        await self.view.handle(self.interaction, 'item', '弓兵:1:武器')
        await self.view.on_timeout()
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.store.gold(1, 1), 1000)
        self.assertNotIn('弓兵:1:武器', self.characters.inventory(1, 1))
