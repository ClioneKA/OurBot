from pathlib import Path
from types import SimpleNamespace
from weakref import WeakSet
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics
from core.rpg_character import CharacterError, Characters
from core.rpg_loadouts import Loadouts
from core.rpg_storage_view import StorageQuantityModal, StorageView
from core.settings import RPGSettings


class StorageRulesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())

    def test_stackables_move_between_backpack_and_storage(self):
        self.characters.grant_item(1, 10, 'paint:red', 7)
        item, quantity = self.characters.store_item(1, 10, 'paint:red', 5)
        self.assertEqual((item.name, quantity), ('紅色噴漆罐', 5))
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 2)
        self.assertEqual(self.characters.storage_entries(1, 10)[0].quantity, 5)

        self.characters.retrieve_item(1, 10, 'paint:red', 3)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 5)
        self.assertEqual(self.characters.storage_entries(1, 10)[0].quantity, 2)
        self.characters.retrieve_item(1, 10, 'paint:red', 2)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 7)
        self.assertEqual(self.characters.storage_entries(1, 10), [])

    def test_invalid_quantity_does_not_partially_move_items(self):
        self.characters.grant_item(1, 10, 'paint:red', 2)
        for amount in (0, -1, 3, 1.5):
            with self.subTest(amount=amount), self.assertRaises(CharacterError):
                self.characters.store_item(1, 10, 'paint:red', amount)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 2)
        self.assertEqual(self.characters.storage_entries(1, 10), [])

    def test_equipment_keeps_identity_sockets_and_affixes(self):
        instance_id = self.characters.grant_item(1, 10, 'noah:archer:weapon:blue')[0]
        token = f'instance:{instance_id}'
        self.characters.set_affixes(1, 10, token, [('test', 'speed', 4)])
        before = self.characters.get_instance(1, 10, token)

        self.characters.store_item(1, 10, token)
        self.assertIsNone(self.characters.get_instance(1, 10, token))
        stored = self.characters.storage_entries(1, 10)[0]
        self.assertEqual((stored.instance_id, stored.item.speed), (instance_id, 4))
        self.assertEqual(self.characters.inventory_counts(1, 10).get(stored.item_id, 0), 0)

        self.characters.retrieve_item(1, 10, token)
        after = self.characters.get_instance(1, 10, token)
        self.assertEqual(after.instance_id, before.instance_id)
        self.assertEqual(after.sockets, before.sockets)
        self.assertEqual(after.affixes, before.affixes)

    def test_equipped_and_loadout_equipment_cannot_be_stored(self):
        self.store.award_voice([(1, 10, level_floor(50))])
        self.characters.change_job(1, 10, '騎士')
        instance_id = self.characters.grant_item(1, 10, 'raid:0')[0]
        token = f'instance:{instance_id}'
        self.characters.equip(1, 10, token)
        with self.assertRaisesRegex(CharacterError, '穿戴'):
            self.characters.store_item(1, 10, token)
        self.characters.unequip(1, 10, '飾品1')
        self.characters.equip(1, 10, token)
        loadouts = Loadouts(self.store, self.characters, Tactics(self.store))
        loadouts.save(1, 10, 1)
        self.characters.unequip(1, 10, '飾品1')
        with self.assertRaisesRegex(CharacterError, '出戰配置'):
            self.characters.store_item(1, 10, token)

    def test_storage_does_not_bypass_one_time_supply_or_shop_ownership(self):
        starter = next(entry.reference for entry in self.characters.inventory_entries(1, 10)
                       if entry.item_id == 'starter:club')
        self.characters.unequip(1, 10, '武器')
        self.characters.store_item(1, 10, starter)
        self.assertEqual(self.characters.claim(1, 10), [])

        self.store.award_voice([(1, 10, level_floor(20))])
        self.characters.change_job(1, 10, '騎士')
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO rpg_wallets VALUES (1,10,5000)')
        item_id = '騎士:1:武器'
        self.characters.buy(1, 10, item_id)
        reference = next(entry.reference for entry in self.characters.inventory_entries(1, 10)
                         if entry.item_id == item_id)
        self.characters.store_item(1, 10, reference)
        with self.assertRaisesRegex(CharacterError, '已經持有'):
            self.characters.buy(1, 10, item_id)

    def test_changing_back_to_job_retrieves_required_early_gear(self):
        self.store.award_voice([(1, 10, level_floor(20))])
        self.characters.change_job(1, 10, '騎士')
        self.characters.change_job(1, 10, '弓兵')
        stored_ids = []
        for entry in self.characters.inventory_entries(1, 10):
            if entry.item_id in ('騎士:0:武器', '騎士:0:套裝'):
                self.characters.store_item(1, 10, entry.reference)
                stored_ids.append(entry.instance_id)
        self.assertEqual(len(stored_ids), 2)
        self.characters.change_job(1, 10, '騎士')
        state = self.characters.snapshot(1, 10)
        self.assertEqual(set(state['equipped_instances'].values()), set(stored_ids))
        self.assertFalse(any(entry.instance_id in stored_ids
                             for entry in self.characters.storage_entries(1, 10)))

    def test_database_failure_rolls_back_stackable_move(self):
        self.characters.grant_item(1, 10, 'paint:red', 4)
        self.store.db.execute('''CREATE TEMP TRIGGER reject_storage BEFORE INSERT ON rpg_storage
            BEGIN SELECT RAISE(ABORT, 'test'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.store_item(1, 10, 'paint:red', 3)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 4)


class StorageViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.characters.grant_item(1, 10, 'paint:red', 6)
        self.cog = SimpleNamespace(characters=self.characters, menu_views=WeakSet())
        self.interaction = SimpleNamespace(
            guild_id=1, user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                     send_modal=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = StorageView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_deposit_and_retrieve_through_quantity_modal(self):
        await self.view.handle(self.interaction, 'item', 'paint:red')
        await self.view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, StorageQuantityModal)
        self.addCleanup(modal.stop)
        modal.amount._value = '4'
        await modal.on_submit(self.interaction)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 2)

        await self.view.handle(self.interaction, 'retrieve')
        await self.view.handle(self.interaction, 'item', 'paint:red')
        await self.view.handle(self.interaction, 'confirm')
        modal = self.interaction.response.send_modal.call_args.args[0]
        self.addCleanup(modal.stop)
        modal.amount._value = '3'
        await modal.on_submit(self.interaction)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 5)
        self.assertIn('取回背包', self.interaction.edit_original_response.call_args.kwargs['embed'].fields[-1].value)

    async def test_stale_modal_cannot_repeat_operation(self):
        await self.view.handle(self.interaction, 'item', 'paint:red')
        modal = StorageQuantityModal(self.view)
        self.addCleanup(modal.stop)
        modal.amount._value = '2'
        await modal.on_submit(self.interaction)
        await modal.on_submit(self.interaction)
        self.assertEqual(self.characters.inventory_counts(1, 10)['paint:red'], 4)
        self.assertIn('設定已變更', self.interaction.response.send_message.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
