from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore, level_floor
from core.rpg_character import Characters, CharacterError
from core.rpg_expeditions import Expeditions, is_expedition_active
from core.rpg_monsters import TIER_VICTORY_XP, prepare_monster
from core.rpg_raid_store import RaidStore
from core.rpg_painted_maze import PaintedMazeStore
from core.settings import RPGSettings


class ExpeditionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = RPGSettings()
        Characters(self.store, self.settings)
        self.store.create_player(1, 1)
        self.expeditions = Expeditions(self.store, self.settings)
        self.raids = RaidStore(self.store)

    def test_level_boundaries_and_exact_reward_ratios(self):
        for level, tier in ((1, 1), (19, 1), (20, 2), (29, 2), (30, 3), (40, 4),
                            (50, 5), (60, 6), (120, 6)):
            with self.store.db:
                self.store.db.execute('UPDATE players SET xp=?', (level_floor(level),))
            gold = 250 if tier >= 5 else 200 if tier >= 3 else 100
            for hours, numerator, proofs in ((4, 6, 2), (8, 9, 3), (12, 12, 4)):
                result = self.expeditions.preview(1, 1, hours)
                self.assertEqual((result['tier'], result['xp'], result['gold'], result['proofs']),
                                 (tier, TIER_VICTORY_XP[tier] * numerator // 8,
                                  gold * numerator // 8, proofs))

    def test_restart_snapshot_claim_once_and_ready_unlock(self):
        session = self.expeditions.start(1, 1, 4, now=100)
        self.assertTrue(is_expedition_active(self.store.db, 1, session['ready_at'] - 1))
        self.assertFalse(is_expedition_active(self.store.db, 1, session['ready_at']))
        self.store.close()
        self.store = RPGStore(self.path)
        self.expeditions = Expeditions(self.store, self.settings)
        self.store.award_voice([(1, 1, level_floor(90))])
        before = self.store.xp(1, 1)
        self.expeditions.finish(1, 1, session['id'], now=session['ready_at'])
        self.assertEqual(self.store.xp(1, 1), before + session['xp'])
        self.assertEqual(self.store.gold(1, 1), session['gold'])
        self.assertEqual(self.store.db.execute("SELECT quantity FROM rpg_inventory WHERE item_id='proof:raid'").fetchone()[0], 2)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(1, 1, session['id'], now=session['ready_at'])

    def test_cancel_no_reward_and_stale_session_cannot_cancel_next(self):
        old = self.expeditions.start(1, 1, 4, now=100)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(1, 1, old['id'], now=101)
        self.expeditions.finish(1, 1, old['id'], cancel=True, now=102)
        new = self.expeditions.start(1, 1, 12, now=103)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(1, 1, old['id'], cancel=True, now=104)
        self.assertEqual(self.expeditions.state(1)['id'], new['id'])
        self.assertEqual((self.store.xp(1, 1), self.store.gold(1, 1)), (0, 0))

    def test_claim_rolls_back_every_reward_on_failure(self):
        session = self.expeditions.start(1, 1, 4, now=0)
        with patch('core.rpg_expeditions.add_owned_item', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.expeditions.finish(1, 1, session['id'], now=14400)
        self.assertEqual((self.store.xp(1, 1), self.store.gold(1, 1)), (0, 0))
        self.assertEqual(self.expeditions.state(1)['id'], session['id'])
        self.expeditions.finish(1, 1, session['id'], now=14400)

    def raid(self):
        raid = self.raids.create(2, 22, prepare_monster(dict(kind='巨獸', name='巨獸'), quality='普通'), 100)
        raid.update(status='lobby', deadline=9999999999)
        self.raids.save(raid)
        return raid

    def test_cross_guild_mutual_exclusion_in_both_directions(self):
        raid = self.raid()
        session = self.expeditions.start(1, 1, 4, now=100)
        with self.assertRaises(CharacterError):
            self.raids.join(raid['id'], 2, 1, 101, 8)
        self.expeditions.finish(1, 1, session['id'], cancel=True, now=102)
        self.raids.join(raid['id'], 2, 1, 103, 8)
        with self.assertRaises(CharacterError):
            self.expeditions.start(1, 1, 4, now=104)

    def test_duplicate_departure_wrong_guild_and_completed_cancellation(self):
        session = self.expeditions.start(1, 1, 4, now=0)
        self.store.create_player(2, 1)
        with self.assertRaises(CharacterError):
            self.expeditions.start(2, 1, 4, now=1)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(2, 1, session['id'], now=14400)
        with self.assertRaises(CharacterError):
            self.expeditions.finish(1, 1, session['id'], cancel=True, now=14400)
        self.raids.join(self.raid()['id'], 2, 1, 14400, 8)

    def test_maze_mutex(self):
        maze = PaintedMazeStore(self.store)
        session = self.expeditions.start(1, 1, 4, now=100)
        with self.assertRaises(CharacterError):
            maze.create(2, 1, 'painting:balloon', 50, now=101, require_entry=False)
        self.expeditions.finish(1, 1, session['id'], cancel=True, now=102)
        maze.create(2, 1, 'painting:balloon', 50, now=103, require_entry=False)
        with self.assertRaises(CharacterError):
            self.expeditions.start(1, 1, 4, now=104)

    def test_fishing_discovery_publishes_without_auto_join(self):
        from core.rpg_fishing import Fishing
        Fishing(self.store)
        self.expeditions.start(1, 1, 4)
        with self.store.db:
            self.store.db.execute("INSERT INTO rpg_fishing_encounters VALUES ('fish',1,1,'pond',0,NULL,'queued')")
        raid = self.raids.create(1, 22,
            prepare_monster(dict(kind='巨獸', name='霸王蟹'), quality='普通'),
            __import__('time').time(), fishing_encounter='fish', pool='special')
        self.assertEqual(raid['members'], [])
        self.assertEqual(raid['discoverer_id'], 1)
        raid['status'] = 'lobby'
        self.raids.save(raid)
        self.assertTrue(is_expedition_active(self.store.db, 1))

    def test_auto_join_save_and_total_raid_mutex(self):
        from core.rpg_total_raids import TotalRaidStore
        from core.rpg_total_battle import TotalRaidError
        total = TotalRaidStore(self.store)
        raid = self.raid()
        session = self.expeditions.start(1, 1, 4)
        raid['members'] = [1]
        with self.assertRaises(CharacterError):
            self.raids.save(raid)
        self.assertEqual(self.raids.get(raid['id'])['members'], [])
        with self.assertRaises(TotalRaidError):
            total.create(2, 20, 21, 1, 'test', 1)
        room = total.create(2, 20, 21, 2, 'test', 1)
        room['members'].append(1)
        with self.assertRaises(TotalRaidError):
            total.save(room)
        self.expeditions.finish(1, 1, session['id'], cancel=True)
        total.save(room)
        with self.assertRaises(CharacterError):
            self.expeditions.start(1, 1, 4)

    def test_removed_duration_is_rejected(self):
        with self.assertRaises(CharacterError):
            self.expeditions.start(1, 1, 24)


class ExpeditionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_cancel_confirmation(self):
        from core.rpg_expedition_view import ExpeditionView
        fixture = ExpeditionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cog = SimpleNamespace(expeditions=fixture.expeditions)
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        view = ExpeditionView(cog, interaction)
        self.addCleanup(view.stop)
        await view.handle(interaction, 'start')
        session = fixture.expeditions.state(1)
        await view.handle(interaction, 'cancel')
        self.assertEqual(fixture.expeditions.state(1)['id'], session['id'])
        self.assertEqual('確認中斷', view.embed().fields[-1].name)
        outsider = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2), response=interaction.response)
        await view.handle(outsider, 'cancel_confirm')
        self.assertIsNotNone(fixture.expeditions.state(1))
        await view.handle(interaction, 'cancel_confirm')
        self.assertIsNone(fixture.expeditions.state(1))
