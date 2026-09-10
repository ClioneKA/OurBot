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

    async def test_exchange_balloon_painting_with_raid_proofs(self):
        self.characters.grant_item(1, 1, 'proof:raid', 30)
        await self.view.handle(self.interaction, 'currency', 'proof')
        await self.view.handle(self.interaction, 'item', 'painting:balloon')
        self.assertEqual(self.view.buy_button.label, '兌換（30 證）')
        self.assertFalse(self.view.buy_button.disabled)
        await self.view.handle(self.interaction, 'buy')
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual(counts.get('proof:raid', 0), 0)
        self.assertEqual(counts['painting:balloon'], 1)
        self.assertEqual(self.store.gold(1, 1), 1000)
        self.assertTrue(self.view.buy_button.disabled)

    async def test_currency_categories_and_expansion_prices(self):
        self.assertIn('expansion:recipe', self.view.catalog)
        self.assertNotIn('expansion:loadout', self.view.catalog)
        self.assertNotIn('painting:balloon', self.view.catalog)
        await self.view.handle(self.interaction, 'item', 'expansion:recipe')
        self.assertTrue(self.view.buy_button.disabled)
        await self.view.handle(self.interaction, 'currency', 'proof')
        self.assertIsNone(self.view.item_id)
        self.assertEqual(self.view.catalog, ['painting:balloon', 'expansion:loadout'])
        self.characters.grant_item(1, 1, 'proof:raid', 400)
        await self.view.handle(self.interaction, 'item', 'expansion:loadout')
        for price in (20, 40, 70, 110, 160):
            self.assertIn(str(price), self.view.buy_button.label)
            await self.view.handle(self.interaction, 'buy')
        self.assertTrue(self.view.buy_button.disabled)
        self.assertIn('已售完', self.view.buy_button.label)
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.characters.inventory_counts(1, 1).get('proof:raid', 0), 0)
        self.assertLessEqual(len(self.view.to_components()), 5)

    async def test_stale_expansion_quote_requires_new_price_confirmation(self):
        self.characters.grant_item(1, 1, 'proof:raid', 100)
        await self.view.handle(self.interaction, 'currency', 'proof')
        await self.view.handle(self.interaction, 'item', 'expansion:loadout')
        self.characters.expansions.buy(1, 1, 'expansion:loadout', expected_purchased=0)
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.characters.inventory_counts(1, 1)['proof:raid'], 80)
        self.assertIn('40', self.view.buy_button.label)
        self.assertIn('價格已變更',
                      self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
        await self.view.handle(self.interaction, 'buy')
        self.assertEqual(self.characters.inventory_counts(1, 1)['proof:raid'], 40)
