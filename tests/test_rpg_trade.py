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
    async def test_bulk_sale_inclusive_across_pages_preserves_other_items(self):
        self.characters.grant_item(1, 1, '騎士:1:武器', 12)
        self.characters.grant_item(1, 1, '騎士:2:武器', 1)
        self.characters.grant_item(1, 1, 'fishing:pond:common', 2)
        self.characters.equip(1, 1, 'raid:0')
        view = TradeView(self.cog, self.interaction, 'sell')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'category', '料理素材')
        await view.handle(self.interaction, 'bulk_tier', '20')
        tier, references, gold = view.bulk_preview
        self.assertEqual((tier, len(references)), (20, 14))
        self.assertEqual(self.store.gold(1, 1), 0)
        self.assertIn('14 件', view.embed().fields[-1].value)
        self.assertLessEqual(len(view.to_components()), 5)
        await view.handle(self.interaction, 'bulk_confirm')
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual(counts['raid:0'], 1)
        self.assertEqual(counts['騎士:2:武器'], 1)
        self.assertEqual(counts['fishing:pond:common'], 2)
        self.assertNotIn('騎士:1:武器', counts)
        self.assertEqual(self.store.gold(1, 1), gold)
        await view.handle(self.interaction, 'bulk_confirm')
        self.assertEqual(self.store.gold(1, 1), gold)
        await view.handle(self.interaction, 'bulk_tier', '20')
        self.assertIsNone(view.bulk_preview)

    async def test_bulk_preview_does_not_sell_new_items_and_refresh_cancels(self):
        view = TradeView(self.cog, self.interaction, 'sell')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'bulk_tier', '20')
        self.characters.grant_item(1, 1, 'raid:0', 1)
        await view.handle(self.interaction, 'bulk_confirm')
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 1)
        await view.handle(self.interaction, 'bulk_tier', '20')
        await view.handle(self.interaction, 'refresh')
        await view.handle(self.interaction, 'bulk_confirm')
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 1)

    async def test_bulk_sale_rolls_back_if_previewed_equipment_changes(self):
        view = TradeView(self.cog, self.interaction, 'sell')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'bulk_tier', '20')
        tier, references, gold = view.bulk_preview
        self.characters.equip(1, 1, references[-1])
        await view.handle(self.interaction, 'bulk_confirm')
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 3)
        self.assertEqual(self.store.gold(1, 1), 0)
        self.characters.unequip(1, 1, '飾品1')
        with self.assertRaises(CharacterError):
            self.characters.sell_equipment_batch(1, 1, references, tier, gold + 1)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 3)
        self.characters.dispose(1, 1, references[-1], 1)
        with self.assertRaises(CharacterError):
            self.characters.sell_equipment_batch(1, 1, references, tier, gold)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 2)
        self.assertEqual(self.store.gold(1, 1), 60)

    async def test_batch_modal_clamps_current_stock_and_sends_one_receipt(self):
        self.characters.grant_item(1, 1, 'fishing:pond:common', 7)
        view = TradeView(self.cog, self.interaction, 'give')
        self.addCleanup(view.stop)
        equipment = next(key for key in view.catalog if view.entries[key].item_id == 'raid:0')
        await view.handle(self.interaction, 'item', [equipment, 'fishing:pond:common'])
        await view.handle(self.interaction, 'recipient', 2)
        await view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.addCleanup(modal.stop)
        self.assertEqual(len(modal.children), 2)
        self.assertEqual(view.children[1].max_values, min(5, len(view.children[1].options)))
        for field in modal.amounts.values():
            field._value = '999'
        self.characters.consume_item(1, 1, 'fishing:pond:common', 3)
        await modal.on_submit(self.interaction)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 1)
        self.assertEqual(self.characters.inventory_counts(1, 2)['fishing:pond:common'], 4)
        member = self.interaction.guild.fetch_member.return_value
        member.send.assert_awaited_once()
        fields = member.send.call_args.kwargs['embed'].fields
        self.assertIn('數量：1', fields[0].value)
        self.assertIn('數量：4', fields[1].value)
        await modal.on_submit(self.interaction)
        member.send.assert_awaited_once()

    async def test_batch_rollback_when_later_item_is_equipped_or_invalid(self):
        self.characters.grant_item(1, 1, 'fishing:pond:common', 7)
        self.characters.equip(1, 1, 'raid:0')
        worn = self.characters.snapshot(1, 1)['equipped_instances']['飾品1']
        for key, amount in [(f'instance:{worn}', 999), ('raid:0', 0), ('raid:0', -1),
                            ('starter:club', 1)]:
            with self.subTest(key=key, amount=amount), self.assertRaises(CharacterError):
                self.characters.give_batch(1, 1, [('fishing:pond:common', 5), (key, amount)], 2)
            self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:pond:common'], 7)
            self.assertNotIn('fishing:pond:common', self.characters.inventory_counts(1, 2))

    async def test_batch_database_failure_rolls_back_previous_items(self):
        self.characters.grant_item(1, 1, 'fishing:pond:common', 7)
        self.store.db.execute("CREATE TEMP TRIGGER reject_batch BEFORE UPDATE ON rpg_equipment_instances WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.give_batch(1, 1, [('fishing:pond:common', 5), ('raid:0', 1)], 2)
        self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:pond:common'], 7)
        self.assertNotIn('fishing:pond:common', self.characters.inventory_counts(1, 2))

    async def test_batch_clamps_equipment_to_unequipped_copies(self):
        self.characters.equip(1, 1, 'raid:0')
        results = self.characters.give_batch(1, 1, [('raid:0', 999)], 2)
        self.assertEqual(results[0][1], 2)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 1)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 2)
        self.assertEqual(self.characters.snapshot(1, 1)['equipped']['飾品1'], 'raid:0')

    async def test_categories_filter_reset_page_and_keep_recipient(self):
        self.characters.grant_item(1, 1, 'fishing:pond:common', 2)
        self.characters.grant_item(1, 1, 'raid:0', 12)
        for mode in ('give', 'sell'):
            with self.subTest(mode=mode):
                view = TradeView(self.cog, self.interaction, mode)
                self.addCleanup(view.stop)
                await view.handle(self.interaction, 'recipient', 2)
                await view.handle(self.interaction, 'next')
                self.assertEqual(view.page, 1)
                await view.handle(self.interaction, 'item', view.catalog[10])
                modal = QuantityModal(view)
                self.addCleanup(modal.stop)
                await view.handle(self.interaction, 'category', '料理素材')
                self.assertEqual(view.catalog, ['fishing:pond:common'])
                self.assertEqual((view.page, view.pages, view.selected, view.recipient), (0, 1, None, 2))
                await view.execute(self.interaction, modal.key, modal.recipient, 1, modal.revision)
                self.assertIn('設定已變更', self.interaction.response.send_message.call_args.args[0])
                await view.handle(self.interaction, 'category', '製作材料')
                self.assertEqual(view.catalog, [])
                self.assertTrue(view.children[1].disabled)
                self.assertLessEqual(len(view.to_components()), 5)
                await view.handle(self.interaction, 'category', '全部')
                self.assertGreater(len(view.catalog), 10)

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.characters.snapshot(1, 1)
        self.store.create_player(1, 2)
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

    async def test_equipped_copy_is_visible_but_cannot_be_confirmed(self):
        self.characters.equip(1, 1, 'raid:0')
        worn = self.characters.snapshot(1, 1)['equipped_instances']['飾品1']
        token = f'instance:{worn}'
        for mode in ('sell', 'give'):
            with self.subTest(mode=mode):
                view = TradeView(self.cog, self.interaction, mode)
                self.addCleanup(view.stop)
                view.recipient = 2
                await view.handle(self.interaction, 'item', token)
                options = {option.value: option for option in view.children[1].options}
                self.assertIn('【已裝備】', options[token].label)
                other = next(key for key in view.catalog
                             if view.entries[key].item_id == 'raid:0' and key != token)
                self.assertNotIn('【已裝備】', options[other].label)
                confirm = next(child for child in view.children if getattr(child, 'label', '') == '填寫數量並確認')
                self.assertTrue(confirm.disabled)
                self.assertIn('請先卸下', view.embed().fields[1].value)
                await view.handle(self.interaction, 'confirm')
                self.interaction.response.send_modal.assert_not_awaited()
        self.characters.unequip(1, 1, '飾品1')
        await view.handle(self.interaction, 'refresh')
        self.assertFalse(view.selected_equipped)
        await view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.addCleanup(modal.stop)

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
        self.store.db.execute("CREATE TEMP TRIGGER reject_gift BEFORE UPDATE ON rpg_equipment_instances WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.dispose(1, 1, 'raid:0', 1, 2)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 3)

    async def test_zero_gold_free_item_is_listed_in_shop_after_unequip(self):
        view = TradeView(self.cog, self.interaction, 'sell')
        self.addCleanup(view.stop)
        self.assertNotIn('starter:club', view.catalog)
        self.characters.unequip(1, 1, '武器')
        view.rebuild()
        starter = next(key for key in view.catalog if view.entries[key].item_id == 'starter:club')
        await view.execute(self.interaction, starter, None, 1, view.revision)
        self.assertNotIn('starter:club', self.characters.inventory(1, 1))
        self.assertEqual(self.characters.claim(1, 1), ['starter:club'])
        self.assertEqual(self.store.gold(1, 1), 0)
        self.assertIn('獲得 0 金幣',
                      self.interaction.edit_original_response.call_args.kwargs['embed'].fields[-1].value)

    async def test_gift_confirmation_replay_and_member_validation(self):
        view = TradeView(self.cog, self.interaction, 'give')
        self.addCleanup(view.stop)
        await view.handle(self.interaction, 'item', 'raid:0')
        await view.handle(self.interaction, 'recipient', 2)
        await view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, QuantityModal)
        self.addCleanup(modal.stop)
        await view.execute(self.interaction, modal.key, modal.recipient, 1, modal.revision)
        await view.execute(self.interaction, modal.key, modal.recipient, 1, modal.revision)
        self.assertEqual(self.characters.inventory_counts(1, 2)['raid:0'], 1)
        self.interaction.guild.fetch_member.assert_awaited_once_with(2)
        self.interaction.guild.fetch_member.return_value.send.assert_awaited_once()
        sent = self.interaction.guild.fetch_member.return_value.send.call_args.kwargs
        self.assertIn('數量：1', sent['embed'].fields[0].value)
        self.interaction.guild.fetch_member.return_value = SimpleNamespace(bot=True)
        await view.execute(self.interaction, 'raid:0', 3, 1, view.revision)
        self.assertEqual(self.characters.inventory_counts(1, 1)['raid:0'], 2)
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
