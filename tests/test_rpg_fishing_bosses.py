import asyncio
import sqlite3
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics, dump_battle, raid_battle
from core.rpg_character import Characters, CharacterError
from core.rpg_divination import Divinations
from core.rpg_fishing import Fishing
from core.rpg_fishing_bosses import FISHING_BOSSES, boss_ingredient
from core.rpg_monsters import prepare_monster
from core.rpg_provisions import FEAST, FORTUNE, evaluate_ingredients
from core.rpg_raids import RaidService
from core.rpg_raid_store import raid_min_level
from core.settings import RPGSettings


class FakeChannel:
    def __init__(self):
        self.id = 99
        self.guild = SimpleNamespace(id=1, unavailable=False,
            get_member=lambda uid: SimpleNamespace(id=uid, display_name='玩家', bot=False))
        self.message = SimpleNamespace(id=100, edit=AsyncMock())
        self.send = AsyncMock(return_value=self.message)

    def get_partial_message(self, message_id):
        return self.message


class FishingBossTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(self.store.close)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.fishing = Fishing(self.store, boss_rng=Mock(random=Mock(return_value=0)))
        self.channel = FakeChannel()
        self.cog = SimpleNamespace(store=self.store, settings=self.settings,
            characters=self.characters, fishing=self.fishing, tactics=Tactics(self.store),
            divinations=Divinations(self.store),
            bot=SimpleNamespace(get_channel=lambda cid: self.channel if cid == 99 else None))
        with patch.dict('os.environ', {'RPG_SPECIAL_RAID_CHANNEL_IDS': '99',
                        'RPG_RAID_CHANNEL_IDS': '', 'RPG_MID_RAID_CHANNEL_IDS': '',
                        'RPG_HIGH_RAID_CHANNEL_IDS': ''}):
            self.service = RaidService(self.cog)
        self.addAsyncCleanup(self.service.close)
        self.channel_patch = patch('core.rpg_fishing_raids.discord.TextChannel', FakeChannel)
        self.channel_patch.start()
        self.addCleanup(self.channel_patch.stop)

    def catch(self, user=1):
        self.store.create_player(1, user)
        self.fishing.start(1, user, 'pond', 'short', now=0)
        return self.fishing.claim(1, user, now=1800)

    def queued(self):
        return self.store.db.execute('SELECT id,status,raid_id FROM rpg_fishing_encounters').fetchall()

    def test_chance_boundary_replay_and_next_session_preserve_encounter(self):
        self.fishing.boss_rng.random.side_effect = [0.001, 0.000999]
        result = self.catch()
        self.assertIn('boss_encounter', result)
        self.assertEqual(self.fishing.boss_rng.random.call_count, 2)
        replay = self.fishing.claim(1, 1, now=1801)
        self.assertEqual(replay['boss_encounter'], result['boss_encounter'])
        self.assertEqual(len(self.queued()), 1)
        self.fishing.start(1, 1, 'pond', 'short', now=1801)
        self.assertEqual(len(self.queued()), 1)
        self.fishing.cancel(1, 1)
        with self.assertRaises(CharacterError):
            self.fishing.claim(1, 1, now=9999)
        self.assertEqual(len(self.queued()), 1)

    def test_failed_claim_rolls_back_loot_and_encounter(self):
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        with self.store.db:
            self.store.db.execute('''CREATE TRIGGER reject_claim BEFORE UPDATE ON rpg_fishing_sessions
                WHEN NEW.status='claimed' BEGIN SELECT RAISE(ABORT, 'test rollback'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.fishing.claim(1, 1, now=1800)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.fishing.state(1, 1)['xp'], 0)
        self.assertEqual(self.fishing.state(1, 1)['session']['status'], 'active')

    def test_misses_do_not_create_encounter(self):
        self.fishing.boss_rng.random.return_value = 0.001
        self.assertNotIn('boss_encounter', self.catch())
        self.assertEqual(self.queued(), [])

    def test_rare_ingredients_follow_existing_portion_duration_and_grade_caps(self):
        for spot_id in FISHING_BOSSES:
            meal = evaluate_ingredients([boss_ingredient(spot_id)] * 5, cooking_level=1)
            self.assertEqual(meal['tag_counts'], {FORTUNE: 5, FEAST: 5})
            self.assertEqual((meal['total_portions'], meal['duration']), (8, 3))
            self.assertEqual(meal['grade_cap'], 'S')
            self.assertIn(meal['grade'], ('D', 'C', 'B', 'A', 'S'))
            self.assertEqual(meal['primary_tag'], FORTUNE)
        recipe = [boss_ingredient('pond')] * 2 + ['fishing:pond:common'] * 2 + ['cooking:seasoning:low']
        meal = evaluate_ingredients(recipe, cooking_level=20)
        self.assertEqual((meal['aftertaste'], meal['duration']), (4, 3))
        self.assertEqual(meal['total_portions'], 5)

    def test_all_bosses_build_and_run_existing_combat(self):
        self.store.create_player(1, 1)
        self.store.award_voice([(1, 1, level_floor(50))])
        self.characters.change_job(1, 1, '騎士')
        participants = [dict(id=1, name='玩家', state=self.characters.snapshot(1, 1), rules=[])]
        for boss in FISHING_BOSSES.values():
            monster = prepare_monster(dict(kind=boss.kind, name=boss.name, description=boss.description), quality='普通')
            battle = raid_battle(participants, monster, 123)
            for _ in range(200):
                if battle.result:
                    break
                battle.step()
            self.assertTrue(battle.result, boss.name)

    async def test_publish_once_finder_joins_and_normal_schedule_unchanged(self):
        self.catch()
        self.service.repo.schedule(2, 42)
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(**kwargs):
            entered.set()
            await release.wait()
            return self.channel.message

        self.channel.send.side_effect = delayed_send
        first = asyncio.create_task(self.service.publish_fishing_encounters())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            await self.service.publish_fishing_encounters()
            self.cog.bot.is_ready = lambda: True
            await self.service.tick()
            self.assertEqual(self.service.repo.pending()[0]['status'], 'posting')
        finally:
            release.set()
            await first
        self.channel.send.assert_awaited_once()
        raid = self.service.repo.pending()[0]
        self.assertEqual((raid['source'], raid['members'], raid_min_level(raid)), ('fishing', [1], 1))
        self.assertEqual(raid['channel_id'], 99)
        self.assertEqual(self.service.repo.next_at(2), 42)
        self.assertNotIn(99, self.service.all_channels)
        self.assertEqual(raid['drop_pool'], [])
        self.assertIsNone(raid['food_drop'])
        self.assertTrue(raid['no_dynamic'])
        self.assertIn('幸運・餘韻・盛宴', str(self.channel.send.call_args.kwargs['embed'].to_dict()))
        await self.service.advance(raid, self.channel, raid['deadline'] - 1)
        self.assertEqual(self.service.repo.get(raid['id'])['status'], 'lobby')

    async def test_missing_channel_busy_guild_and_fifo(self):
        self.catch(1)
        self.catch(2)
        self.cog.bot.get_channel = lambda cid: None
        await self.service.publish_fishing_encounters()
        self.assertTrue(all(r[1] == 'queued' for r in self.queued()))
        self.cog.bot.get_channel = lambda cid: self.channel
        self.service.spawning_guilds.add(1)
        await self.service.publish_fishing_encounters()
        self.channel.send.assert_not_awaited()
        self.service.spawning_guilds.clear()
        await self.service.publish_fishing_encounters()
        await self.service.publish_fishing_encounters()
        self.channel.send.assert_awaited_once()
        self.assertEqual(sum(row[1] == 'queued' for row in self.queued()), 1)

    async def test_failed_post_retries_but_cancelled_published_raid_does_not(self):
        self.catch()
        self.channel.send.side_effect = RuntimeError('offline')
        with self.assertLogs('core.rpg_fishing_raids', level='ERROR'):
            await self.service.publish_fishing_encounters()
        self.assertEqual(self.service.repo.pending(), [])
        self.channel.send.side_effect = None
        await self.service.publish_fishing_encounters()
        raid = self.service.repo.pending()[0]
        raid.update(status='cancelled', delivered=True)
        self.service.repo.save(raid)
        await self.service.publish_fishing_encounters()
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_posting_crash_recovers_after_reopening_database(self):
        result = self.catch()
        boss = FISHING_BOSSES['pond']
        monster = prepare_monster(dict(kind=boss.kind, name=boss.name, description='test'), quality='普通')
        raid = self.service.repo.create(1, 99, monster, 1, asdict(self.settings.raid),
            pool='special', use_dynamic=False, fishing_encounter=result['boss_encounter']['id'])
        other = RPGStore(self.path)
        self.addCleanup(other.close)
        Fishing(other)
        from core.rpg_raid_store import RaidStore
        self.service.repo = RaidStore(other)
        self.cog.bot.add_view = Mock()
        with patch.object(self.service.tick, 'start'):
            self.service.start()
        self.assertEqual(self.service.repo.get(raid['id'])['status'], 'cancelled')
        await self.service.publish_fishing_encounters()
        self.channel.send.assert_awaited_once()
        self.assertNotEqual(self.service.repo.pending()[0]['id'], raid['id'])

    async def test_victory_rewards_once_and_defeat_has_no_ingredient(self):
        for victory in (True, False):
            result = self.catch(1)
            await self.service.publish_fishing_encounters()
            raid = self.service.repo.pending()[0]
            self.store.create_player(1, 2)
            raid.update(status='running', participants=[dict(id=uid, name=str(uid),
                state=self.characters.snapshot(1, uid), rules=[]) for uid in (1, 2)])
            self.service.repo.save(raid)
            battle = dump_battle(raid_battle(raid['participants'], raid['monster'], raid['seed']))
            battle['result'] = '勝利' if victory else '戰敗'
            before = [self.characters.inventory_counts(1, uid).get(boss_ingredient('pond'), 0) for uid in (1, 2)]
            settled = self.service.repo.settle(raid['id'], battle, self.settings.raid)
            self.service.repo.settle(raid['id'], battle, self.settings.raid)
            after = [self.characters.inventory_counts(1, uid).get(boss_ingredient('pond'), 0) for uid in (1, 2)]
            self.assertEqual([a-b for a, b in zip(after, before)], [2, 1] if victory else [0, 0])
            self.assertTrue(all('raid_proofs' not in r for r in settled['rewards']))
            settled['delivered'] = True
            self.service.repo.save(settled)
