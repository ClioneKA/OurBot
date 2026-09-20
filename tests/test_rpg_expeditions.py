import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore, level_floor
from core.rpg_alchemy import (AlchemyDolls, body_material_id, material_profile,
                              operation_fuel_cost)
from core.rpg_character import Characters, CharacterError, ITEMS, item_sellable
from core.rpg_expeditions import (Expeditions, is_doll_expedition_active,
                                  is_legacy_expedition_active)
from core.rpg_monsters import TIER_VICTORY_XP, prepare_monster
from core.rpg_raid_store import RaidStore
from core.settings import RPGSettings


class ExpeditionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.characters.create(1, 1)
        self.alchemy = AlchemyDolls(self.store, self.settings)
        self.body = dict(tier=10, stats=[28, 28, 28, 28, 28])
        with self.store.db:
            self.alchemy._ensure(1, 1)
            self.store.db.execute('''UPDATE rpg_alchemy_dolls SET active_body=?,fuel=1000
                WHERE guild_id=1 AND user_id=1''', (json.dumps(self.body),))
        self.expeditions = Expeditions(self.store, self.settings)
        self.raids = RaidStore(self.store)

    def fuel(self, guild=1, user=1):
        return self.alchemy.state(guild, user)['fuel']

    def test_gold_uses_frozen_body_stats_and_duration(self):
        cost = operation_fuel_cost(self.body)
        for hours in (1, 2, 3):
            result = self.expeditions.preview(1, 1, hours, 'gold')
            self.assertEqual(result['gold'], 70 * hours)
            self.assertEqual(result['fuel'], cost * hours)
            self.assertEqual(result['remaining_fuel'], 1000 - cost * hours)

    def test_material_route_has_one_stat_tendency_and_progression_cap(self):
        with self.store.db:
            self.store.db.execute('UPDATE players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(60),))
        result = self.expeditions.preview(1, 1, 3, 'precision')
        self.assertEqual((result['material_tier'], result['quantity']), (20, 3))
        self.assertEqual(result['material'], body_material_id(20, 3))
        self.assertEqual(material_profile(result['material']),
                         (20, (0.0, 0.0, 0.0, 1.0, 0.0)))
        self.assertTrue(ITEMS[result['material']].transferable)
        self.assertFalse(item_sellable(ITEMS[result['material']]))
        with self.assertRaisesRegex(CharacterError, '不能轉換'):
            self.alchemy.convert_fuel(1, 1, result['material'], 1)

        body = dict(tier=60, stats=[136] * 5)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=? '
                                  'WHERE guild_id=1 AND user_id=1', (json.dumps(body),))
            self.store.db.execute('UPDATE players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(30),))
        self.assertEqual(self.expeditions.preview(1, 1, 1, 'structure')['material_tier'], 30)

    def test_start_charges_fuel_and_claims_frozen_gold_once(self):
        session = self.expeditions.start(1, 1, 2, 'gold', now=100)
        self.assertEqual(self.fuel(), 1000 - session['fuel'])
        self.assertTrue(is_doll_expedition_active(self.store.db, 1, 1,
                                                  session['ready_at'] - 1))
        self.assertFalse(is_doll_expedition_active(self.store.db, 1, 1,
                                                   session['ready_at']))
        with self.store.db:
            changed = dict(self.body, stats=[200] * 5)
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=? '
                                  'WHERE guild_id=1 AND user_id=1', (json.dumps(changed),))
        result = self.expeditions.finish(1, 1, session['id'], now=session['ready_at'])
        self.assertEqual(result['gold'], 140)
        self.assertEqual(self.store.gold(1, 1), 140)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(1, 1, session['id'], now=session['ready_at'])

    def test_material_claim_is_atomic_and_cancel_does_not_refund_fuel(self):
        session = self.expeditions.start(1, 1, 3, 'spirit', now=0)
        after_start = self.fuel()
        with patch('core.rpg_expeditions.add_owned_item', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.expeditions.finish(1, 1, session['id'], now=session['ready_at'])
        self.assertIsNotNone(self.expeditions.state(1, 1))
        self.expeditions.finish(1, 1, session['id'], cancel=True,
                                now=session['ready_at'] - 1)
        self.assertEqual(self.fuel(), after_start)

    def test_per_guild_dolls_can_travel_independently_and_players_can_raid(self):
        self.characters.create(2, 1)
        with self.store.db:
            self.alchemy._ensure(2, 1)
            self.store.db.execute('''UPDATE rpg_alchemy_dolls SET active_body=?,fuel=1000
                WHERE guild_id=2 AND user_id=1''', (json.dumps(self.body),))
        first = self.expeditions.start(1, 1, 1, now=100)
        second = self.expeditions.start(2, 1, 2, now=100)
        self.assertEqual(self.expeditions.state(1, 1)['id'], first['id'])
        self.assertEqual(self.expeditions.state(2, 1)['id'], second['id'])

        raid = self.raids.create(
            1, 22, prepare_monster(dict(kind='巨獸', name='巨獸'), quality='普通'), 101)
        raid.update(status='lobby', deadline=9999999999)
        self.raids.save(raid)
        self.raids.join(raid['id'], 1, 1, 102, 8)
        self.assertIn(1, self.raids.get(raid['id'])['members'])

    def test_automation_and_support_are_unavailable_until_return(self):
        session = self.expeditions.start(1, 1, 1, now=100)
        with self.assertRaisesRegex(CharacterError, '正在遠征'):
            self.alchemy._reserve_operation(1, 1, 'fishing', 'test', 1, 101)
        self.assertIsNone(self.alchemy.prepare_support(1, 1, 'raid:test', now=101))
        receipt = self.alchemy._reserve_operation(
            1, 1, 'fishing', 'after', 1, session['ready_at'])
        self.assertEqual(receipt['status'], 'reserved')

    def test_missing_body_bad_duration_and_insufficient_fuel_are_rejected(self):
        with self.assertRaises(CharacterError):
            self.expeditions.start(1, 1, 4)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=NULL '
                                  'WHERE guild_id=1 AND user_id=1')
        with self.assertRaisesRegex(CharacterError, '安裝素體'):
            self.expeditions.start(1, 1, 1)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=?,fuel=0 '
                                  'WHERE guild_id=1 AND user_id=1', (json.dumps(self.body),))
        with self.assertRaisesRegex(CharacterError, '燃料不足'):
            self.expeditions.start(1, 1, 1)

    def test_running_doll_support_blocks_departure_but_lobby_does_not(self):
        raid = self.raids.create(
            1, 22, prepare_monster(dict(kind='巨獸', name='巨獸'), quality='普通'), 100)
        raid.update(status='lobby', members=[1], deadline=9999999999)
        self.raids.save(raid)
        session = self.expeditions.start(1, 1, 1, now=101)
        self.expeditions.finish(1, 1, session['id'], cancel=True, now=102)
        raid.update(status='running', participants=[{'id': 1, 'doll_support': {'name': '人偶'}}])
        self.raids.save(raid)
        with self.assertRaisesRegex(CharacterError, '正在戰鬥'):
            self.expeditions.start(1, 1, 1, now=103)

    def test_legacy_session_keeps_old_reward_and_player_mutex(self):
        legacy = dict(id='legacy', guild_id=1, user_id=1, status='active', hours=4,
                      level=10, tier=1, proofs=2, xp=TIER_VICTORY_XP[1] * 6 // 8,
                      gold=75, started_at=0, ready_at=14400)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_expeditions VALUES (?,?,?,?,?,?)',
                                  ('legacy', 1, 1, 'active', 14400, json.dumps(legacy)))
        self.assertTrue(is_legacy_expedition_active(self.store.db, 1, 100))
        raid = self.raids.create(
            1, 22, prepare_monster(dict(kind='巨獸', name='巨獸'), quality='普通'), 100)
        raid.update(status='lobby', deadline=9999999999)
        self.raids.save(raid)
        with self.assertRaises(CharacterError):
            self.raids.join(raid['id'], 1, 1, 101, 8)
        self.expeditions.finish(1, 1, 'legacy', now=14400)
        self.assertEqual(self.store.gold(1, 1), 75)
        self.assertEqual(self.store.db.execute(
            "SELECT quantity FROM rpg_inventory WHERE item_id='proof:raid'").fetchone()[0], 2)


class ExpeditionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_route_and_cancel_confirmation(self):
        from core.rpg_expedition_view import ExpeditionView
        fixture = ExpeditionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cog = SimpleNamespace(expeditions=fixture.expeditions)
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        view = ExpeditionView(cog, interaction)
        self.addCleanup(view.stop)
        await view.handle(interaction, 'route', 'precision')
        await view.handle(interaction, 'duration', '2')
        await view.handle(interaction, 'start')
        session = fixture.expeditions.state(1, 1)
        self.assertEqual((session['route'], session['hours']), ('precision', 2))
        await view.handle(interaction, 'cancel')
        self.assertEqual('確認中斷', view.embed().fields[-1].name)
        outsider = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
                                  response=interaction.response)
        await view.handle(outsider, 'cancel_confirm')
        self.assertIsNotNone(fixture.expeditions.state(1, 1))
        await view.handle(interaction, 'cancel_confirm')
        self.assertIsNone(fixture.expeditions.state(1, 1))


if __name__ == '__main__':
    unittest.main()
