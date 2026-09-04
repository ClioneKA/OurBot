from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore
from core.rpg_character import Characters, CharacterError
from core.rpg_trade_view import TradeView, QuantityModal
from core.settings import RPGSettings


class TradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.characters.snapshot(1, 1)
        with self.store.db:
            self.store.db.execute("INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity) VALUES (1,1,'raid:0',3)")
        self.cog = SimpleNamespace(characters=self.characters, store=self.store, menu_views=WeakSet())
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            guild=SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False, send=AsyncMock()))),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock()),
            edit_original_response=AsyncMock())

    async def test_transfer_and_sale_preserve_equipped_copy(self):
        self.characters.equip(1, 1, 'raid:0')
        self.characters.dispose(1, 1, 'raid:0', 1, 2)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 1)
        self.assertEqual(self.characters.dispose(1, 1, 'raid:0', 1), 60)
        self.assertEqual(self.store.gold(1, 1), 60)
        with self.assertRaises(CharacterError):
            self.characters.dispose(1, 1, 'raid:0', 1)
        self.assertEqual(self.characters.snapshot(1, 1)['equipped']['飾品1'], 'raid:0')
        self.characters.unequip(1, 1, '飾品1')
        self.characters.dispose(1, 1, 'raid:0', 1)
        self.assertNotIn('raid:0', self.characters.inventory(1, 1))
        self.assertNotIn('raid:0', self.characters.inventory(2, 2))

    async def test_invalid_actions_and_atomic_sale_rollback(self):
        for key, amount, recipient in (('starter:club', 1, 2), ('raid:0', 0, None),
                                       ('raid:0', 4, None), ('raid:0', 1, 1)):
            with self.assertRaises(CharacterError):
                self.characters.dispose(1, 1, key, amount, recipient)
        self.store.db.execute("CREATE TEMP TRIGGER reject_sale BEFORE INSERT ON rpg_wallets BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.dispose(1, 1, 'raid:0', 2)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 3)
        self.assertEqual(self.store.gold(1, 1), 0)
        self.store.db.execute("CREATE TEMP TRIGGER reject_gift BEFORE INSERT ON rpg_inventory WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.dispose(1, 1, 'raid:0', 1, 2)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 3)

    async def test_gift_confirmation_replay_and_member_validation(self):
        view = TradeView(self.cog, self.interaction, 'give')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'item', 'raid:0')
        await view.handle(self.interaction, 'recipient', 2)
        await view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, QuantityModal)
        self.addCleanup(modal.stop)
        await view.execute(self.interaction, modal.key, modal.recipient, 2, modal.revision)
        await view.execute(self.interaction, modal.key, modal.recipient, 2, modal.revision)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 2)
        self.interaction.guild.fetch_member.assert_awaited_once_with(2)
        self.interaction.guild.fetch_member.return_value.send.assert_awaited_once()
        sent = self.interaction.guild.fetch_member.return_value.send.call_args.kwargs
        self.assertIn('數量：2', sent['embed'].fields[0].value)
        self.interaction.guild.fetch_member.return_value = SimpleNamespace(bot=True)
        await view.execute(self.interaction, 'raid:0', 3, 1, view.revision)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 1)
        self.assertIn('機器人', self.interaction.edit_original_response.call_args.kwargs['embed'].fields[-1].value)

    async def test_sell_modal_stale_and_unauthorized_then_success(self):
        view = TradeView(self.cog, self.interaction, 'sell')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'item', 'raid:0')
        modal = QuantityModal(view)
        self.addCleanup(modal.stop)
        await view.handle(self.interaction, 'refresh')
        await view.execute(self.interaction, modal.key, None, 1, modal.revision)
        self.assertEqual(self.store.gold(1, 1), 0)
        other = SimpleNamespace(guild_id=2, user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=AsyncMock()))
        await view.execute(other, 'raid:0', None, 1, view.revision)
        self.assertEqual(self.store.gold(1, 1), 0)
        await view.execute(self.interaction, 'raid:0', None, 3, view.revision)
        self.assertEqual(self.store.gold(1, 1), 180)

        self.assertIsNone(view.selected)
        self.assertLessEqual(len(view.to_components()), 5)
        await view.on_timeout()
        await view.execute(self.interaction, 'raid:0', None, 1, view.revision)
        self.assertEqual(self.store.gold(1, 1), 180)

    async def test_failed_dm_does_not_rollback_or_repeat_gift(self):
        import discord
        member = self.interaction.guild.fetch_member.return_value
        member.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Cannot send messages')
        view = TradeView(self.cog, self.interaction, 'give')
        self.addCleanup(view.stop)
        await view.execute(self.interaction, 'raid:0', 2, 1, 0)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 1)
        self.assertIn('私訊通知未能送達', self.interaction.edit_original_response.call_args.kwargs['embed'].fields[-1].value)
        await view.execute(self.interaction, 'raid:0', 2, 1, 0)
        member.send.assert_awaited_once()
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 1)
