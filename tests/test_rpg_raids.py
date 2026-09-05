from dataclasses import asdict, replace
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics, dump_battle, raid_battle, load_battle
from core.rpg_character import Characters, CharacterError
from core.rpg_raids import RaidService, RaidSignup, channel_ids
from core.rpg_raid_store import RaidStore, DROP_TABLES
from core.settings import RPGSettings, RaidSettings, SettingsError


class RaidTests(unittest.IsolatedAsyncioTestCase):
    async def test_mid_tier_channel_pool_rewards_and_fixed_paint_drop(self):
        from cmds.rpg import RPG
        choices = next(p.choices for p in RPG.spawn_raid.parameters if p.name == 'kind')
        self.assertTrue({'深淵鐘龍', '王城傀儡師', '瘟疫縫合獸'} <= {choice.value for choice in choices})

        class FakeChannel:
            id = 3
            guild = SimpleNamespace(id=1, unavailable=False)

        channel = FakeChannel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=9))
        with patch.dict('os.environ', {'RPG_RAID_CHANNEL_IDS': '2', 'RPG_MID_RAID_CHANNEL_IDS': '3'}):
            service = RaidService(self.cog)
        service.notifications.ensure = AsyncMock(return_value=None)
        with patch('core.rpg_raids.discord.TextChannel', FakeChannel), patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                patch('core.rpg_monsters.random.choices', return_value=['普通']):
            with self.assertRaises(CharacterError):
                await service.spawn(channel, kind='巨獸')
            await service.spawn(channel, kind='深淵鐘龍')
        raid = next(item for item in service.repo.pending() if item['channel_id'] == 3)
        self.assertEqual((raid['pool'], raid['monster']['tier']), ('mid', 3))
        self.assertEqual((raid['reward_policy']['victory_xp'], raid['reward_policy']['victory_gold']), (600, 200))
        self.assertEqual(raid['fixed_drop'], 'paint:red')
        participant = self.participant()
        raid.update(status='running', participants=[participant], members=[1])
        service.repo.save(raid)
        battle = raid_battle([participant], raid['monster'], 1)
        battle.result = '勝利'
        settled = service.repo.settle(raid['id'], dump_battle(battle), self.settings.mid_raid)
        self.assertEqual(settled['rewards'][0]['fixed_item'], 'paint:red')
        self.assertEqual(self.characters.inventory_counts(1, 1)['paint:red'], 1)

        team_raid = self.repo.create(1, 3, raid['monster'], 200,
                                     asdict(replace(self.settings.mid_raid, drop_chance=0)))
        team = [self.participant(uid) for uid in (11, 12, 13)]
        team_raid.update(status='running', participants=team, members=[11, 12, 13])
        self.repo.save(team_raid)
        team_battle = raid_battle(team, team_raid['monster'], 2)
        team_battle.result = '勝利'
        team_result = self.repo.settle(team_raid['id'], dump_battle(team_battle), self.settings.mid_raid)
        winners = [reward for reward in team_result['rewards'] if reward.get('fixed_item') == 'paint:red']
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(self.characters.inventory_counts(1, uid).get('paint:red', 0)
                             for uid in (11, 12, 13)), 1)
        self.assertIn('全隊固定掉落 1 個 紅色噴漆罐', self.service.lobby_embed(team_raid).fields[-1].value)

    async def test_mid_tier_shuffle_bag_contains_each_monster_once(self):
        kinds = ('深淵鐘龍', '王城傀儡師', '瘟疫縫合獸')
        first = [self.repo.next_mid_kind(99, kinds) for _ in range(3)]
        second = [self.repo.next_mid_kind(99, kinds) for _ in range(3)]
        self.assertEqual(set(first), set(kinds))
        self.assertEqual(set(second), set(kinds))

    async def test_fox_bat_generation_loot_and_command_choices(self):
        from core.rpg_monsters import prepare_monster
        from cmds.rpg import RPG
        choices = next(p.choices for p in RPG.spawn_raid.parameters if p.name == 'kind')
        for kind, size in (('月影妖狐', 1), ('血翼蝠王', 4)):
            self.assertIn(kind, [c.value for c in choices])
            with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
                monster = prepare_monster(await self.service.imagine(kind))
            self.assertEqual(monster['tier'], 2)
            raid = self.repo.create(1, 2, monster, 100, asdict(self.settings.raid), {'drop_chance': 1})
            self.assertEqual(len(raid['drop_pool']), size)
            self.assertEqual(raid['drop_pool'], list(DROP_TABLES[kind]))
            self.assertIn('150%', self.service.lobby_embed(raid).fields[2].value)
            p = self.participant()
            raid.update(status='running', participants=[p], members=[1])
            self.repo.save(raid)
            battle = raid_battle([p], monster, 1)
            battle.result = '勝利'
            result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
            self.assertIn(result['rewards'][0]['item'], raid['drop_pool'])

    async def test_goblin_generation_lobby_and_exclusive_loot(self):
        from core.rpg_monsters import prepare_monster
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            monster = await self.service.imagine('哥布林戰團')
        monster = prepare_monster(monster)
        self.assertEqual(monster['tier'], 2)
        raid = self.repo.create(1, 2, monster, 100, asdict(self.settings.raid), {'drop_chance': 1})
        self.assertEqual(raid['drop_pool'], list(DROP_TABLES['哥布林戰團']))
        self.assertEqual(len(raid['drop_pool']), 5)
        self.assertIn('鼓舞', self.service.lobby_embed(raid).fields[2].value)
        p = self.participant()
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        battle = raid_battle([p], monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertIn(result['rewards'][0]['item'], raid['drop_pool'])

    async def test_tree_generation_and_four_job_suit_loot(self):
        from core.rpg_character import ITEMS
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            monster = await self.service.imagine('荊棘妖樹')
        self.assertEqual(monster['kind'], '荊棘妖樹')
        raid = self.repo.create(1, 2, monster, 100, asdict(replace(self.settings.raid, drop_chance=1)))
        pool = raid['drop_pool']
        self.assertEqual(len(pool), 4)
        self.assertEqual({ITEMS[key].job for key in pool}, {'裝甲步兵', '騎士', '弓兵', '僧侶'})
        self.assertTrue(all(ITEMS[key].slot == '套裝' and ITEMS[key].price == 0 and ITEMS[key].stage == 1 for key in pool))
        self.assertIn('33%', self.service.lobby_embed(raid).fields[2].value)
        p = self.participant()
        raid.update(status='running', participants=[p], members=[1])
        self.repo.save(raid)
        battle = raid_battle([p], monster, 1)
        battle.result = '勝利'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertIn(result['rewards'][0]['item'], pool)

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
                self.store.db.execute(
                    'INSERT INTO rpg_raid_difficulty(guild_id,channel_id,multiplier,balance_version) '
                    'VALUES (?,?,?,1)', (1, channel, multiplier))
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

    async def test_v2_difficulty_is_narrow_performance_aware_and_ignores_rare_quality(self):
        from core.rpg_monsters import prepare_monster

        participant = self.participant()

        def finish(result, round_number, remaining_percent=0, quality='普通'):
            monster = prepare_monster(dict(self.monster), quality=quality)
            raid = self.repo.create(1, 8, monster, 100, asdict(self.settings.raid))
            raid.update(status='running', participants=[participant], members=[1])
            self.repo.save(raid)
            battle = raid_battle([participant], raid['monster'], 1)
            enemy = battle.living(1)[0]
            enemy.hp = enemy.stats['HP'] * remaining_percent // 100
            battle.result = result
            battle.round = round_number
            return self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)

        with self.store.db:
            self.store.db.execute(
                'INSERT INTO rpg_raid_difficulty(guild_id,channel_id,multiplier,balance_version) '
                'VALUES (1,8,2.5,1)')
        first = finish('勝利', 12)
        self.assertEqual(first['difficulty']['current'], 1)
        self.assertEqual(self.repo.difficulty(1, 8), 1.03)
        finish('勝利', 25)
        self.assertEqual(self.repo.difficulty(1, 8), 1.03)
        finish('戰敗', 12, remaining_percent=80)
        self.assertEqual(self.repo.difficulty(1, 8), 0.9991)
        finish('勝利', 10, quality='首領')
        self.assertEqual(self.repo.difficulty(1, 8), 0.9991)

        with self.store.db:
            self.store.db.execute(
                'UPDATE rpg_raid_difficulty SET multiplier=1.1,balance_version=2 '
                'WHERE guild_id=1 AND channel_id=8')
        finish('勝利', 5)
        self.assertEqual(self.repo.difficulty(1, 8), 1.1)

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
        notify_role = SimpleNamespace(id=123, mention='<@&123>')
        self.service.notifications.ensure = AsyncMock(return_value=notify_role)
        self.service.imagine = AsyncMock(return_value=dict(self.monster, kind='史萊姆群'))
        with patch('core.rpg_raids.discord.TextChannel', FakeChannel):
            await self.service.spawn(channel, kind='史萊姆群', name='寶藏史萊姆群', strength=2.0,
                                     victory_xp=1000, victory_gold=500, drop_percent=100)
        self.service.imagine.assert_awaited_once_with('史萊姆群')
        sent = channel.send.call_args.kwargs
        self.assertEqual(sent['content'], '<@&123>')
        self.assertEqual(sent['allowed_mentions'].roles, [notify_role])
        self.assertFalse(sent['allowed_mentions'].everyone)
        self.assertFalse(sent['allowed_mentions'].users)
        raid = self.repo.pending()[0]
        self.assertEqual(raid['monster']['name'], '寶藏史萊姆群')
        self.assertEqual(raid['reward_policy']['victory_xp'], 1000)
        self.assertEqual(raid['reward_policy']['victory_gold'], 500)
        self.assertEqual(raid['reward_policy']['drop_chance'], 0)
        p = self.participant()
        regular = raid_battle([p], dict(raid['monster'], strength=1, manual_strength=1), 1)
        custom = raid_battle([p], raid['monster'], 1)
        self.assertEqual(sum(f.stats['HP'] for f in custom.living(1)),
                         2 * sum(f.stats['HP'] for f in regular.living(1)))
        for stat in ('攻擊', '防禦'):
            self.assertEqual(custom.fighters[-1].stats[stat], 2 * regular.fighters[-1].stats[stat])
        self.assertEqual(custom.fighters[-1].stats['暴擊率'], 5)
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
        choice.assert_called_once_with(('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王'), weights=(20, 20, 5, 11, 11, 11, 11, 11), k=1)
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

    async def test_slime_defeat_at_full_hp_has_no_rewards(self):
        monster = dict(self.monster, kind='史萊姆群')
        raid = self.repo.create(1, 2, monster, 100, asdict(self.settings.raid))
        raid.update(status='running', participants=[self.participant()], members=[1])
        self.repo.save(raid)
        battle = raid_battle(raid['participants'], monster, 1)
        battle.result = '戰敗'
        result = self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)
        self.assertEqual(result['rewards'], [dict(id=1, xp=0, gold=0, item=None)])

    async def test_failure_rewards_use_final_combined_hp_and_frozen_policy(self):
        for index, result_name in enumerate(('戰敗', '平手（達回合上限）', '平手')):
            with self.subTest(result=result_name):
                uid = 100 + index
                raid = self.repo.create(1, 200 + index, self.monster, 100,
                                        asdict(replace(self.settings.raid, victory_xp=101,
                                                       victory_gold=53, drop_chance=1.0)))
                raid.update(status='running', participants=[self.participant(uid)], members=[uid])
                self.repo.save(raid)
                battle = raid_battle(raid['participants'], self.monster, 1)
                data = dump_battle(battle)
                enemy = data['fighters'][-1]
                enemy['stats']['HP'], enemy['hp'] = 100, 0
                import copy
                second = copy.deepcopy(enemy)
                second['stats']['HP'], second['hp'] = 200, 101
                data['fighters'].append(second)
                data['result'] = result_name
                settled = self.repo.settle(raid['id'], data,
                                           replace(self.settings.raid, victory_xp=9999, victory_gold=9999))
                self.assertEqual(settled['rewards'], [dict(id=uid, xp=66, gold=35, item=None)])
                self.assertEqual(settled['failure_progress'], dict(max_hp=300, remaining_hp=101))
                self.repo.settle(raid['id'], data, self.settings.raid)
                self.assertEqual((self.store.xp(1, uid), self.store.gold(1, uid)), (66, 35))
                self.assertEqual(self.characters.inventory(1, uid), ['starter:club'])

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
        self.service.notifications.ensure = AsyncMock(return_value=None)
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

    async def test_settlement_persists_queryable_balance_metrics(self):
        participant = self.participant()
        raid = self.repo.create(1, 2, self.monster, 100, asdict(self.settings.raid))
        raid.update(status='running', participants=[participant], members=[1])
        self.repo.save(raid)
        battle = raid_battle([participant], self.monster, 1)
        player = battle.fighters[0]
        player.combat_stats.update(damage_dealt=321, direct_damage=280, support_damage=41,
                                   damage_taken=45, healing_done=67,
                                   attacks=5, hits=4, misses=1, critical_hits=2)
        player.combat_stats['skills_used'] = {'奮力一擊': 2}
        battle.result = '勝利'

        self.repo.settle(raid['id'], dump_battle(battle), self.settings.raid)

        stored = self.store.db.execute('''SELECT damage_dealt, direct_damage, support_damage,
            damage_taken, healing_done,
            attacks, hits, misses, critical_hits, skills_used
            FROM rpg_battle_participants WHERE raid_id=? AND user_id=?''',
                                       (raid['id'], 1)).fetchone()
        self.assertEqual(stored, (321, 280, 41, 45, 67, 5, 4, 1, 2, '{"奮力一擊": 2}'))
        report = self.repo.balance_report(1, 0)
        self.assertEqual(report['overall'][:2], (1, 1))
        self.assertEqual(report['monsters'][0][:3], ('巨獸', 1, 1))
        self.assertEqual(report['jobs'][0][:3], ('民兵', 1, 1))

    async def test_deadline_repeat_capacity_exit_and_guild_checks(self):
        raid = self.lobby()
        self.assertEqual(raid['deadline'], 400)
        self.repo.join(raid['id'], 1, 1, 399, 1)
        for guild, uid, now in ((1, 1, 399), (1, 2, 399), (2, 2, 399), (1, 2, 400)):
            with self.assertRaises(CharacterError):
                self.repo.join(raid['id'], guild, uid, now, 1)
        self.repo.join(raid['id'], 1, 1, 399, 1, leave=True)
        self.assertEqual(self.repo.get(raid['id'])['members'], [])

    async def test_mid_tier_requires_level_30_but_regular_does_not(self):
        regular = self.lobby()
        self.repo.join(regular['id'], 1, 1, 101, 20)

        mid = self.repo.create(1, 3, dict(self.monster, kind='深淵鐘龍'), 100, pool='mid')
        mid.update(status='lobby', message_id=4)
        self.repo.save(mid)
        self.assertIn('需 Lv.30', self.service.lobby_embed(mid).fields[1].name)
        with self.assertRaisesRegex(CharacterError, r'Lv\.30'):
            self.repo.join(mid['id'], 1, 2, 101, 20)

        with self.store.db:
            self.store.db.execute('INSERT INTO players(guild_id,user_id,xp) VALUES (?,?,?)',
                                  (1, 2, level_floor(30)))
        self.repo.join(mid['id'], 1, 2, 101, 20)
        self.assertEqual(self.repo.get(mid['id'])['members'], [2])

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
        battle.fighters[-1].hp = battle.fighters[-1].stats['HP'] // 2
        raid['battle'] = dump_battle(battle)
        self.repo.save(raid)
        self.message.edit.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError):
            await self.service.advance(raid, self.channel, 400)
        state = self.repo.get(raid['id'])
        self.assertFalse(state['delivered'])
        self.assertEqual(self.store.xp(1, 1), 150)
        self.assertEqual(self.store.gold(1, 1), 50)
        self.message.edit.side_effect = None
        await self.service.advance(state, self.channel, 405)
        self.assertEqual(self.store.xp(1, 1), 150)
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
        self.assertIn(monster['kind'], ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王'))
        self.assertTrue(monster['name'])
        self.assertEqual(channel_ids('2, 2, 3'), {2, 3})
        for raw in ('abc', '-1'):
            with self.assertRaises(ValueError):
                channel_ids(raw)
        with self.assertRaises(SettingsError):
            RaidSettings(min_interval_minutes=100, max_interval_minutes=10)
        for periods in ('12:00', '24:00-01:00', '12:00-12:00'):
            with self.assertRaises(SettingsError):
                RaidSettings(half_interval_periods=periods)

    async def test_next_spawn_halves_interval_only_when_drawn_in_configured_period(self):
        self.service.settings = replace(
            self.settings.raid, min_interval_minutes=30, max_interval_minutes=90,
            half_interval_periods='12:00-14:00,22:00-02:00', schedule_timezone_offset_hours=8)
        cases = [
            ('2026-09-05T12:30:00+08:00', 900, 2700),
            ('2026-09-05T14:00:00+08:00', 1800, 5400),
            ('2026-09-05T23:30:00+08:00', 900, 2700),
            ('2026-09-05T01:30:00+08:00', 900, 2700),
        ]
        for timestamp, minimum, maximum in cases:
            now = datetime.fromisoformat(timestamp).timestamp()
            with self.subTest(timestamp=timestamp), patch('core.rpg_raids.random.randint', return_value=minimum) as draw:
                self.service.next_spawn(2, now)
                draw.assert_called_once_with(minimum, maximum)
                self.assertEqual(self.repo.next_at(2), now + minimum)

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
