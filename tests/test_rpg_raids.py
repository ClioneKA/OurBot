from dataclasses import asdict, replace
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore
from core.rpg_battle import Tactics, dump_battle, raid_battle, load_battle
from core.rpg_character import Characters, CharacterError
from core.rpg_raids import RaidService, RaidSignup, channel_ids
from core.rpg_raid_store import RaidStore, DROP_TABLES
from core.settings import RPGSettings, RaidSettings, SettingsError


class RaidTests(unittest.IsolatedAsyncioTestCase):
    async def test_golem_generation_stats_and_exclusive_loot(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            monster = await self.service.imagine('鐵殼魔像')
        self.assertEqual(monster['kind'], '鐵殼魔像')
        self.assertIn('魔像', monster['name'])
        p = self.participant()
        basic = raid_battle([p], self.monster, 1).fighters[-1]
        golem = raid_battle([p], monster, 1).fighters[-1]
        self.assertEqual(golem.stats['HP'], int(basic.stats['HP'] * 1.2))
        self.assertEqual(golem.stats['防禦'], basic.stats['防禦'] * 2)
        self.assertEqual(golem.stats['攻擊'], basic.stats['攻擊'])
        raid = self.repo.create(1, 2, monster, 100, asdict(replace(self.settings.raid, drop_chance=1)))
        self.assertEqual(raid['drop_pool'], ['golem:hammer', 'golem:sword_shield', 'golem:bow', 'golem:staff'])
        from core.rpg_character import ITEMS
        self.assertTrue(all(ITEMS[key].slot == '武器' for key in raid['drop_pool']))
        self.assertIn('專屬裝備', self.service.lobby_embed(raid).fields[-1].value)
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        battle = raid_battle([p], monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertIn(result['rewards'][0]['item'], raid['drop_pool'])
        self.store.award_voice([(1, 2, 200000000)])
        for job in ('騎士', '裝甲步兵', '弓兵', '僧侶'):
            self.characters.change_job(1, 2, job)
            self.characters.claim(1, 2)
        self.assertFalse(any(key.startswith('golem:') for key in self.characters.inventory(1, 2)))

    async def test_type_bound_loot_tables_and_future_equipment(self):
        self.assertEqual(self.settings.raid.drop_chance, 0.25)
        for kind in ('巨獸', '毒蛛'):
            self.assertEqual(DROP_TABLES[kind], tuple(f'raid:{i}' for i in range(5)))
        p = self.participant()
        for channel, kind, pool in ((30, '新怪物', ()), (31, '新裝備怪物', ('騎士:1:武器',)),
                                    (32, '新套裝怪物', ('騎士:1:套裝',))):
            monster = dict(self.monster, kind=kind)
            with patch.dict(DROP_TABLES, {kind: pool} if pool else {}):
                raid = self.repo.create(1, channel, monster, 100,
                                        asdict(replace(self.settings.raid, drop_chance=1)))
            self.assertEqual(raid['drop_pool'], list(pool))
            raid.update(status='running', participants=[p], members=[1])
            self.repo.save(raid)
            battle = raid_battle([p], monster, 1)
            battle.result = '勝利'
            result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
            self.assertEqual(result['rewards'][0]['item'], pool[0] if pool else None)
        self.assertFalse(any(key.startswith('raid:') for key in self.characters.inventory(1, 1)))

    async def test_difficulty_scales_announced_rewards_and_preserves_overrides(self):
        policy = asdict(self.settings.raid)
        cases = [(0.5, '巨獸', {}, 150, 50), (1.5, '巨獸', {}, 450, 150),
                 (3.0, '巨獸', {}, 900, 300), (1.5, '史萊姆群', {}, 900, 300),
                 (1.5, '史萊姆群', {'victory_xp': 7}, 7, 300),
                 (1.5, '巨獸', {'victory_xp': 0, 'victory_gold': 9}, 0, 9),
                 (1.089, '巨獸', {}, 326, 108)]
        for channel, (multiplier, kind, overrides, xp, gold) in enumerate(cases, 20):
            with self.store.db:
                self.store.db.execute('INSERT INTO rpg_raid_difficulty VALUES (?,?,?)', (1, channel, multiplier))
            raid = self.repo.create(1, channel, dict(self.monster, kind=kind, strength=2), 100, policy, overrides)
            saved = self.repo.get(raid['id'])
            self.assertEqual((saved['reward_policy']['victory_xp'], saved['reward_policy']['victory_gold']), (xp, gold))
            self.assertEqual(saved['reward_policy']['defeat_xp'], 30)
            self.assertEqual(saved['reward_policy']['drop_chance'], 0 if kind == '史萊姆群' else 0.25)
            self.assertIn(f'{xp} XP', self.service.lobby_embed(saved).fields[-1].value)
            p = self.participant()
            saved.update(status='running', participants=[p], members=[1])
            self.repo.save(saved)
            battle = raid_battle([p], saved['monster'], 1)
            battle.result = '勝利'
            result = self.repo.settle(saved['id'], dump_battle(battle), replace(self.settings.raid, victory_xp=999))
            self.assertEqual((result['rewards'][0]['xp'], result['rewards'][0]['gold']), (xp, gold))
        self.assertEqual(policy, asdict(self.settings.raid))

    async def test_dynamic_difficulty_progression_limits_and_persistence(self):
        p = self.participant()
        policy = asdict(replace(self.settings.raid, drop_chance=0))
        def finish(result, strength=1):
            monster = dict(self.monster, strength=strength)
            raid = self.repo.create(1, 2, monster, 100, policy)
            current = self.repo.difficulty(1, 2)
            self.assertEqual(raid['monster']['strength'], round(current * strength, 6))
            self.assertEqual(monster['strength'], strength)
            raid.update(status='running', participants=[p], members=[1])
            self.repo.save(raid)
            battle = raid_battle([p], raid['monster'], 1)
            battle.result = result
            settled = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
            after = self.repo.difficulty(1, 2)
            self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
            self.assertEqual(self.repo.difficulty(1, 2), after)
            self.assertEqual(settled['difficulty_change']['after'], after)
            return settled
        self.assertEqual(self.repo.difficulty(1, 2), 1)
        finish('勝利')
        self.assertEqual(self.repo.difficulty(1, 2), 1.1)
        finished = finish('戰敗', 2)
        self.assertEqual(finished['monster']['strength'], 2.2)
        self.assertEqual(self.repo.difficulty(1, 2), 0.99)
        finish('平手（達回合上限）')
        self.assertEqual(self.repo.difficulty(1, 2), 0.891)
        for _ in range(20):
            finish('戰敗')
        self.assertEqual(self.repo.difficulty(1, 2), 0.5)
        for _ in range(25):
            finish('勝利')
        self.assertEqual(self.repo.difficulty(1, 2), 3)
        self.assertEqual(self.repo.difficulty(1, 3), 1)
        self.assertEqual(self.repo.difficulty(2, 2), 1)
        path = self.store.db.execute('PRAGMA database_list').fetchone()[2]
        reopened = RPGStore(path)
        try:
            self.assertEqual(RaidStore(reopened).difficulty(1, 2), 3)
        finally:
            reopened.close()

    async def test_dynamic_difficulty_cancellation_and_atomic_rollback(self):
        raid = self.lobby()
        await self.service.advance(raid, self.channel, 400)
        self.assertEqual(self.repo.difficulty(1, 2), 1)
        raid = self.lobby()
        p = self.participant()
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        battle = raid_battle([p], raid['monster'], 1)
        battle.result = '勝利'
        self.store.db.execute("CREATE TEMP TRIGGER reject_difficulty BEFORE INSERT ON rpg_raid_difficulty BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.repo.difficulty(1, 2), 1)
        self.assertEqual(self.store.xp(1, 1), 0)
        self.assertEqual(self.store.gold(1, 1), 0)
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])
        self.assertEqual(self.repo.get(raid['id'])['status'], 'running')
        self.store.db.execute('DROP TRIGGER reject_difficulty')
        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.repo.difficulty(1, 2), 1.1)

    async def test_manual_options_persist_without_changing_defaults(self):
        class FakeChannel:
            id = 2
            guild = SimpleNamespace(id=1, unavailable=False)
        channel = FakeChannel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=3))
        self.service.imagine = AsyncMock(return_value=dict(self.monster, kind='史萊姆群'))
        with patch('core.rpg_raids.discord.TextChannel', FakeChannel):
            await self.service.spawn(channel, kind='史萊姆群', name='寶藏史萊姆群', strength=2.0,
                                     victory_xp=1000, victory_gold=500, drop_percent=100)
        self.service.imagine.assert_awaited_once_with('史萊姆群')
        raid = self.repo.pending()[0]
        self.assertEqual(raid['monster']['name'], '寶藏史萊姆群')
        self.assertEqual(raid['reward_policy']['victory_xp'], 1000)
        self.assertEqual(raid['reward_policy']['victory_gold'], 500)
        self.assertEqual(raid['reward_policy']['drop_chance'], 0)
        p = self.participant()
        regular = raid_battle([p], self.monster, 1)
        custom = raid_battle([p], raid['monster'], 1)
        for stat in ('HP', '攻擊', '防禦'):
            self.assertEqual(custom.fighters[-1].stats[stat], 2 * regular.fighters[-1].stats[stat])
        self.assertEqual(custom.fighters[-1].stats['暴擊率'], 10)
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        custom.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(custom), self.settings.raid)
        self.assertEqual(result['rewards'], [dict(id=1, xp=1000, gold=500, item=None)])
        self.assertEqual(self.service.settings.victory_xp, 300)
        self.assertEqual(self.service.settings.drop_chance, 0.25)

    async def test_invalid_manual_options_do_not_generate_monsters(self):
        self.service.imagine = AsyncMock()
        for options in (dict(kind='不存在'), dict(name=' '), dict(strength=0),
                        dict(strength=float('nan')), dict(victory_xp=-1), dict(drop_percent=101)):
            with self.assertRaises(CharacterError):
                await self.service.spawn(None, **options)
        self.service.imagine.assert_not_awaited()

    async def test_slime_rarity_fallback_and_saved_rewards(self):
        with patch('core.rpg_raids.random.choices', return_value=['史萊姆群']) as choice, patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            monster = await self.service.imagine()
        choice.assert_called_once_with(('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像'), weights=(35, 35, 10, 20), k=1)
        self.assertIn('史萊姆群', monster['name'])
        policy = asdict(replace(self.settings.raid, victory_xp=400, victory_gold=150, drop_chance=1.0))
        raid = self.repo.create(1, 2, monster, 100, policy)
        self.assertEqual(policy['victory_xp'], 400)
        self.assertEqual(raid['reward_policy']['drop_chance'], 0)
        text = self.service.lobby_embed(raid).fields[-1].value
        self.assertIn('800 XP', text)
        self.assertIn('300 金幣', text)
        self.assertIn('不掉落飾品', text)
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(result['rewards'], [dict(id=1, xp=800, gold=300, item=None)])
        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.store.gold(1, 1), 300)
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])

    async def test_slime_defeat_has_normal_xp_and_no_loot(self):
        monster = dict(self.monster, kind='史萊姆群')
        raid = self.repo.create(1, 2, monster, 100, asdict(self.settings.raid))
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], monster, 1)
        battle.result = '戰敗'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(result['rewards'], [dict(id=1, xp=30, gold=0, item=None)])

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.tactics = Tactics(self.store)
        self.cog = SimpleNamespace(store=self.store, settings=self.settings, characters=self.characters,
                                   tactics=self.tactics, ai_model='unused', bot=SimpleNamespace())
        with patch.dict('os.environ', {'RPG_RAID_CHANNEL_IDS': '2'}):
            self.service = RaidService(self.cog)
        self.repo = self.service.repo
        self.monster = dict(name='怪獸', description='安安構思的魔物', kind='巨獸')
        self.message = SimpleNamespace(edit=AsyncMock())
        self.channel = SimpleNamespace(guild=SimpleNamespace(get_member=lambda uid: SimpleNamespace(id=uid, display_name='玩家', bot=False)),
                                       get_partial_message=lambda mid: self.message)

    async def asyncTearDown(self):
        await self.service.close()

    def lobby(self, now=100):
        raid = self.repo.create(1, 2, self.monster, now)
        raid.update(status='lobby', message_id=3)
        self.repo.save(raid)
        return raid

    def participant(self, uid=1):
        return dict(id=uid, name='玩家', state=self.characters.snapshot(1, uid),
                    rules=[asdict(r) for r in self.tactics.rules(1, uid, '民兵')])

    async def test_deadline_repeat_capacity_exit_and_guild_checks(self):
        raid = self.lobby()
        self.assertEqual(raid['deadline'], 400)
        self.repo.join(raid['id'], 1, 1, 399, 1)
        for guild, uid, now in ((1, 1, 399), (1, 2, 399), (2, 2, 399), (1, 2, 400)):
            with self.assertRaises(CharacterError):
                self.repo.join(raid['id'], guild, uid, now, 1)
        self.repo.join(raid['id'], 1, 1, 399, 1, leave=True)
        self.assertEqual(self.repo.get(raid['id'])['members'], [])

    async def test_no_early_start_then_resumable_battle_and_rewards(self):
        raid = self.lobby()
        self.repo.join(raid['id'], 1, 1, 101, 20)
        await self.service.advance(self.repo.get(raid['id']), self.channel, 399)
        self.assertEqual(self.repo.get(raid['id'])['status'], 'lobby')
        await self.service.advance(self.repo.get(raid['id']), self.channel, 400)
        state = self.repo.get(raid['id'])
        self.assertEqual(state['status'], 'running')
        self.assertEqual(len(state['battle']['fighters']), 2)
        # New service reads persisted battle, with no NPCs or in-memory roster dependency.
        with patch.dict('os.environ', {'RPG_RAID_CHANNEL_IDS': '2'}):
            resumed = RaidService(self.cog)
        try:
            for turn in range(31):
                state = self.repo.get(raid['id'])
                if state['delivered']:
                    break
                await resumed.advance(state, self.channel, 405 + turn * 5)
            state = self.repo.get(raid['id'])
            self.assertEqual(state['status'], 'completed')
            self.assertTrue(state['delivered'])
            self.assertEqual(self.store.xp(1, 1), state['rewards'][0]['xp'])
            self.assertIn('attachments', self.message.edit.call_args.kwargs)
        finally:
            await resumed.close()

    async def test_empty_lobby_cancels_without_reward(self):
        raid = self.lobby()
        await self.service.advance(raid, self.channel, 400)
        self.assertEqual(self.repo.get(raid['id'])['status'], 'cancelled')
        self.assertEqual(self.store.xp(1, 1), 0)

    async def test_rewards_atomic_idempotent_and_loot_not_supplies(self):
        raid = self.lobby()
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], self.monster, 1)
        battle.result = '勝利'
        policy = replace(self.settings.raid, drop_chance=1.0)
        state = self.repo.settle(raid['id'], dump_battle(battle), policy)
        item = state['rewards'][0]['item']
        self.assertTrue(item.startswith('raid:'))
        self.assertEqual(self.characters.inventory(1, 1), [item, 'starter:club'])
        self.repo.settle(raid['id'], dump_battle(battle), policy)
        self.assertEqual(self.store.xp(1, 1), policy.victory_xp)
        self.assertEqual(self.store.gold(1, 1), 100)
        self.assertEqual(state['rewards'][0]['gold'], 100)
        self.assertEqual(self.store.gold(2, 1), 0)
        self.assertEqual(self.store.gold(1, 2), 0)
        self.assertEqual(self.characters.inventory(1, 1), [item, 'starter:club'])
        self.assertEqual(self.characters.inventory(2, 1), ['starter:club'])

    async def test_failure_rewards_and_retry_after_delivery_failure(self):
        raid = self.lobby()
        raid.update(status='running', participants=[self.participant()], members=[1])
        battle = raid_battle(raid['participants'], self.monster, 1)
        battle.result = '戰敗'
        raid['battle'] = dump_battle(battle)
        self.repo.save(raid)
        self.message.edit.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError):
            await self.service.advance(raid, self.channel, 400)
        state = self.repo.get(raid['id'])
        self.assertFalse(state['delivered'])
        self.assertEqual(self.store.xp(1, 1), 30)
        self.assertEqual(self.store.gold(1, 1), 0)
        self.message.edit.side_effect = None
        await self.service.advance(state, self.channel, 405)
        self.assertEqual(self.store.xp(1, 1), 30)
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])

    async def test_skill_priority_and_job_isolation(self):
        self.tactics.configure(1, 1, '騎士', 3, 1, False, 'self40', 'self')
        rules = Tactics(self.store).rules(1, 1, '騎士')
        self.assertEqual([r.priority for r in rules], [1, 2, 3])
        self.assertEqual((rules[0].slot, rules[0].enabled), (3, False))
        self.assertTrue(self.tactics.rules(1, 1, '僧侶')[0].enabled)
        self.assertTrue(self.tactics.rules(2, 1, '騎士')[0].enabled)
        with self.assertRaises(CharacterError):
            self.tactics.configure(1, 1, '弓兵', 1, 1, True, 'always', 'self')

    async def test_imagination_fallback_and_channel_validation(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            monster = await self.service.imagine()
        self.assertIn(monster['kind'], ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像'))
        self.assertTrue(monster['name'])
        self.assertEqual(channel_ids('2, 2, 3'), {2, 3})
        for raw in ('abc', '-1'):
            with self.assertRaises(ValueError):
                channel_ids(raw)
        with self.assertRaises(SettingsError):
            RaidSettings(min_interval_minutes=100, max_interval_minutes=10)

    async def test_scheduler_only_configured_channel_and_single_announcement(self):
        class FakeChannel:
            id = 2
            guild = SimpleNamespace(id=1, unavailable=False)
            send = AsyncMock(return_value=SimpleNamespace(id=9))
        channel = FakeChannel()
        channel.get_partial_message = lambda mid: self.message
        self.service.bot = SimpleNamespace(is_ready=lambda: True, get_channel=lambda cid: channel if cid == 2 else None)
        self.service.imagine = AsyncMock(return_value=self.monster)
        self.repo.schedule(2, 0)
        with patch('core.rpg_raids.discord.TextChannel', FakeChannel):
            await self.service.tick.coro(self.service)
            await self.service.tick.coro(self.service)
        channel.send.assert_awaited_once()
        raid = self.repo.pending()[0]
        self.assertEqual((raid['channel_id'], raid['status']), (2, 'lobby'))
        self.assertIsInstance(channel.send.call_args.kwargs['view'], RaidSignup)
        self.assertGreater(self.repo.next_at(2), 0)

    async def test_manual_spawn_rejects_overlap_and_cleans_up_failed_send(self):
        class FakeChannel:
            id = 2
            guild = SimpleNamespace(id=1, unavailable=False)
        channel = FakeChannel()
        channel.send = AsyncMock(side_effect=RuntimeError('network'))
        entered, release = asyncio.Event(), asyncio.Event()
        async def imagine():
            entered.set()
            await release.wait()
            return self.monster
        self.service.imagine = imagine
        with patch('core.rpg_raids.discord.TextChannel', FakeChannel):
            first = asyncio.create_task(self.service.spawn(channel))
            await entered.wait()
            try:
                with self.assertRaises(CharacterError):
                    await self.service.spawn(channel)
            finally:
                release.set()
            with self.assertRaises(RuntimeError):
                await first
            self.assertEqual(self.repo.pending(), [])
            self.assertEqual(self.service.spawning, set())
            self.assertEqual(self.service.spawn_tasks, set())
            channel.id = 99
            with self.assertRaises(CharacterError):
                await self.service.spawn(channel)
            channel.id = 2
            self.service.settings = replace(self.settings.raid, enabled=False)
            with self.assertRaises(CharacterError):
                await self.service.spawn(channel)

    async def test_signup_button_and_no_cross_raid_registration(self):
        raid = self.lobby()
        interaction = SimpleNamespace(guild_id=1, channel_id=2, user=SimpleNamespace(id=1, bot=False),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        view = self.service.signup(raid)
        with patch('core.rpg_raids.time.time', return_value=101):
            await view.respond(interaction, False)
        self.assertEqual(self.repo.get(raid['id'])['members'], [1])
        other = self.repo.create(1, 3, self.monster, 100)
        other['status'] = 'lobby'
        self.repo.save(other)
        with self.assertRaises(CharacterError):
            self.repo.join(other['id'], 1, 1, 102, 20)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create(1, 2, self.monster, 100)

    async def test_reward_failure_rolls_back_loot_and_preserves_announced_policy(self):
        raid = self.repo.create(1, 2, self.monster, 100,
                                asdict(replace(self.settings.raid, victory_xp=123, drop_chance=1.0)))
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], self.monster, 1)
        battle.result = '勝利'
        self.store.db.execute("CREATE TEMP TRIGGER reject_reward BEFORE INSERT ON players BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])
        self.assertEqual(self.repo.get(raid['id'])['status'], 'running')
        self.store.db.execute('DROP TRIGGER reject_reward')
        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.store.xp(1, 1), 123)

    async def test_supplies_never_grant_raid_loot(self):
        self.store.award_voice([(1, 1, 200000000)])
        self.characters.change_job(1, 1, '騎士')
        self.assertFalse(any(item.startswith('raid:') for item in self.characters.inventory(1, 1)))

    async def test_raid_only_drops_accessories_including_owned_copies(self):
        self.store.award_voice([(1, 1, 4470)])
        self.characters.change_job(1, 1, '騎士')
        with self.store.db:
            self.store.db.executemany('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,?)', [(f'raid:{i}',) for i in range(5)])
        p = self.participant()
        p['state'] = self.characters.snapshot(1, 1)
        raid = self.lobby()
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        battle = raid_battle([p], self.monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), replace(self.settings.raid, drop_chance=1.0))
        item = result['rewards'][0]['item']
        self.assertTrue(item.startswith('raid:'))
        self.assertEqual(self.characters.inventory_counts(1, 1)[item], 2)
        self.assertEqual(result['rewards'][0]['xp'], self.settings.raid.victory_xp)
        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.characters.inventory_counts(1, 1)[item], 2)
        self.assertIn(item, self.characters.inventory(1, 1))
        self.assertFalse(any(':2:' in key or ':3:' in key for key in self.characters.inventory(1, 1)))

    async def test_gold_policy_persistence_accumulation_and_atomicity(self):
        raid = self.repo.create(1, 2, self.monster, 100,
                                asdict(replace(self.settings.raid, victory_gold=77, drop_chance=1.0)))
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], self.monster, 1)
        battle.result = '勝利'
        self.store.db.execute("CREATE TEMP TRIGGER reject_gold BEFORE INSERT ON rpg_wallets BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual((self.store.gold(1, 1), self.store.xp(1, 1)), (0, 0))
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])
        self.assertEqual(self.repo.get(raid['id'])['status'], 'running')
        self.store.db.execute('DROP TRIGGER reject_gold')
        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.store.gold(1, 1), 77)
        second = self.repo.create(1, 2, self.monster, 200, asdict(self.settings.raid))
        second.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(second)
        self.repo.settle(second['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(self.store.gold(1, 1), 187)
        path = self.store.db.execute('PRAGMA database_list').fetchone()[2]
        reopened = RPGStore(path)
        try:
            self.assertEqual(reopened.gold(1, 1), 187)
        finally:
            reopened.close()

    async def test_legacy_raid_without_gold_keeps_original_reward(self):
        policy = asdict(self.settings.raid)
        policy.pop('victory_gold')
        raid = self.repo.create(1, 2, self.monster, 100, policy)
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], self.monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(result['rewards'][0]['gold'], 0)
        self.assertEqual(self.store.gold(1, 1), 0)

    async def test_ai_payload_validation_and_fallback(self):
        self.service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='{"name":"月影獸","description":"吾輩的怪物來了"}'))), close=AsyncMock())
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-unused'}):
            result = await self.service.imagine()
            self.assertEqual(result['name'], '月影獸')
            self.service.client.responses.create.return_value = SimpleNamespace(output_text='invalid JSON')
            result = await self.service.imagine()
            self.assertTrue(result['name'])
