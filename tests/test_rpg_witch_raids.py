import json
import time
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from core.rpg_character import ITEMS, add_owned_item
from core.rpg_total_battle import ACTION_ATTACK, load_total_battle, dump_total_battle
from core.rpg_total_raids import TotalRaidError, TotalRaidStore, TotalRaidService, witch_day, WitchDailyView
from core.rpg_witch_catalog import WITCH_BOSS
from core.rpg_witch_battle import WitchRaidBattle
from core.rpg_witch_embroideries import REQUIREMENTS, record_victory
from core.rpg_total_raids import WitchBattleView, WitchPrivateActionView, WitchActionButton, WitchPrivateTargetSelect, active_effect_notes, field_chunks, effect_status
from tests import test_rpg_witch_battle as battle_fixtures
from tests.test_rpg_total_raids import TotalRaidRoomTests, HashableMember, FakeChannel, FakeCategory
import discord
from types import SimpleNamespace


class WitchRoomTests(TotalRaidRoomTests):
    def reward_room(self, channel_id, members, finished_at, result='勝利', guild_id=1):
        room = self.service.repo.create(guild_id, 50, channel_id, members[0], WITCH_BOSS, channel_id)
        room.update(status='completed', members=members, finished_at=finished_at,
                    witch_day='2026-09-09', battle={'result': result})
        self.service.repo.save(room)
        return room, SimpleNamespace(result=result, ids=['anan', 'noah', 'meruru'])

    async def test_daily_reward_across_rooms_restart_guilds_and_midnight(self):
        before = datetime(2026, 9, 9, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        first, battle = self.reward_room(101, [1], before)
        self.service.repo.finish_witch(first, battle)
        original_text = first['reward_text']
        self.service.repo.finish_witch(first, battle)
        self.assertEqual(first['reward_text'], original_text)
        self.service.repo = TotalRaidStore(self.store)
        second, battle = self.reward_room(102, [1, 2], before)
        self.service.repo.finish_witch(second, battle)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 1)
        self.assertEqual(self.characters.available_quantity(1, 2, 'witch:thread'), 1)
        self.assertIn('<@2>', second['reward_text'])
        self.assertNotIn('<@1>', second['reward_text'])
        other_guild, battle = self.reward_room(103, [1], before, guild_id=2)
        self.service.repo.finish_witch(other_guild, battle)
        self.assertEqual(self.characters.available_quantity(2, 1, 'witch:thread'), 0)
        next_day, battle = self.reward_room(104, [1], before + 1)
        self.service.repo.finish_witch(next_day, battle)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 2)

    async def test_loss_does_not_consume_daily_reward_and_failed_grant_rolls_back(self):
        now = time.time()
        lost, battle = self.reward_room(101, [1], now, result='敗北')
        self.service.repo.finish_witch(lost, battle)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rpg_witch_daily_rewards').fetchone()[0], 0)
        won, battle = self.reward_room(102, [1, 2], now)
        real_add = add_owned_item
        def fail_second(db, guild, uid, item, quantity):
            if uid == 2:
                raise RuntimeError('grant failed')
            return real_add(db, guild, uid, item, quantity)
        with patch('core.rpg_total_raids.add_owned_item', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'grant failed'):
                self.service.repo.finish_witch(won, battle)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rpg_witch_daily_rewards').fetchone()[0], 0)
        self.service.repo.finish_witch(won, battle)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 1)
        self.assertEqual(self.characters.available_quantity(1, 2, 'witch:thread'), 1)

    async def test_existing_paid_rooms_backfill_daily_claims(self):
        now = time.time()
        paid, _ = self.reward_room(101, [1], now)
        lost, _ = self.reward_room(102, [2], now, result='敗北')
        with self.store.db:
            self.store.db.executemany('INSERT INTO rpg_witch_rewards VALUES (?)', [(paid['id'],), (lost['id'],)])
            add_owned_item(self.store.db, 1, 1, 'witch:thread', 1)
            self.store.db.execute('DROP TABLE rpg_witch_daily_rewards')
        self.service.repo = TotalRaidStore(self.store)
        again, battle = self.reward_room(103, [1, 2], now)
        self.service.repo.finish_witch(again, battle)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 1)
        self.assertEqual(self.characters.available_quantity(1, 2, 'witch:thread'), 1)

    def announcement_fixture(self, existing=False):
        guild = SimpleNamespace(id=1, default_role=object(), me=object())
        channel = FakeChannel(80, guild)
        channel.name = '魔女試煉'
        category = FakeCategory(50, channel)
        category.guild = guild
        category.text_channels = [channel] if existing else []
        self.bot.channels[50] = category
        self.service.witch_channel_ids = set()
        return category, channel

    async def test_auto_channel_uses_shared_category_and_persists_after_rename(self):
        category, channel = self.announcement_fixture()
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory), patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.announce_witches()
            self.assertEqual(category.create_text_channel.call_args.kwargs['name'], '魔女試煉')
            channel.send.assert_awaited_once()
            self.bot.channels[80] = channel
            channel.name = '已改名的公告'
            restarted = TotalRaidService(self.cog)
            restarted.category_ids = {50}
            restarted.witch_channel_ids = set()
            await restarted.announce_witches()
        category.create_text_channel.assert_awaited_once()
        channel.send.assert_awaited_once()

    async def test_reuses_existing_named_channel_and_respects_explicit_ids(self):
        category, channel = self.announcement_fixture(existing=True)
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory), patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            self.assertEqual(await self.service.witch_announcement_channels(), [channel])
            self.service.witch_channel_ids = {90}
            explicit = FakeChannel(90, channel.guild)
            self.bot.channels[90] = explicit
            self.assertEqual(await self.service.witch_announcement_channels(), [explicit])
        category.create_text_channel.assert_not_awaited()
        channel.edit.assert_awaited_once()
        explicit.edit.assert_awaited_once()

    async def test_announcement_lock_preserves_visibility_and_blocks_role_overrides(self):
        category, channel = self.announcement_fixture(existing=True)
        role = object()
        channel.overwrites = {
            channel.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              send_messages_in_threads=True),
        }
        await self.service.lock_witch_announcement(channel)
        desired = channel.edit.call_args.kwargs['overwrites']
        self.assertFalse(desired[channel.guild.default_role].view_channel)
        self.assertTrue(desired[role].view_channel)
        for target in (channel.guild.default_role, role):
            for permission in ('send_messages', 'create_public_threads',
                               'create_private_threads', 'send_messages_in_threads'):
                self.assertIs(getattr(desired[target], permission), False)
        self.assertTrue(desired[channel.guild.me].send_messages)
        self.assertTrue(channel.overwrites[role].send_messages)
        channel.overwrites = desired
        await self.service.lock_witch_announcement(channel)
        channel.edit.assert_awaited_once()

    async def test_existing_channel_lock_failure_backs_off(self):
        category, channel = self.announcement_fixture(existing=True)
        channel.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no permission')
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory), patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), patch('core.rpg_total_raids.logger.exception'):
            self.assertEqual(await self.service.witch_announcement_channels(), [])
            self.assertEqual(await self.service.witch_announcement_channels(), [])
        channel.edit.assert_awaited_once()
        category.create_text_channel.assert_not_awaited()

    async def test_cache_miss_fetches_before_recreating_deleted_channel(self):
        category, channel = self.announcement_fixture()
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_witch_channels VALUES (1,80)')
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory), patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            self.assertEqual(await self.service.witch_announcement_channels(), [channel])
            category.create_text_channel.assert_not_awaited()
            self.bot.fetch_channel.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Missing'), 'deleted')
            replacement = FakeChannel(81, channel.guild)
            category.create_text_channel.return_value = replacement
            self.assertEqual(await self.service.witch_announcement_channels(), [replacement])
        category.create_text_channel.assert_awaited_once()
        self.assertEqual(self.store.db.execute('SELECT channel_id FROM rpg_witch_channels WHERE guild_id=1').fetchone()[0], 81)

    async def test_missing_permissions_back_off_without_spamming_creation(self):
        category, channel = self.announcement_fixture()
        category.create_text_channel.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no permission')
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory), patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), patch('core.rpg_total_raids.logger.exception'):
            self.assertEqual(await self.service.witch_announcement_channels(), [])
            self.assertEqual(await self.service.witch_announcement_channels(), [])
        category.create_text_channel.assert_awaited_once()

    async def test_late_action_resolves_timeout_and_result_delivery_retries(self):
        room, host, channel = await self.setup_witch_room()
        with self.store.db:
            add_owned_item(self.store.db, 1, 1, 'proof:raid', 1)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            room = await self.service.begin(room['id'], host)
            room['round_deadline'] = 0
            self.service.repo.save(room)
            with self.assertRaisesRegex(TotalRaidError, '已逾時'):
                await self.service.submit_action(room['id'], 1, ACTION_ATTACK, 'e:1', None)
            room = self.service.repo.get(room['id'])
            self.assertEqual(load_total_battle(room['battle']).round, 1)
            # Simulate a persisted result whose Discord delivery was interrupted.
            room['public_pending'] = True
            self.service.repo.save(room)
            await self.service.cleanup_witch_rooms(room['created_at']+1)
            self.assertFalse(self.service.repo.get(room['id'])['public_pending'])

    async def test_taiwan_day_boundary_and_restart_roster(self):
        before = datetime(2026, 9, 9, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(witch_day(before), '2026-09-09')
        self.assertEqual(witch_day(before+1), '2026-09-10')
        a = self.service.repo.daily_witches(before)
        self.assertEqual(a, TotalRaidStore(self.store).daily_witches(before))
        self.assertEqual(len(set(a[1])), 3)
        self.assertNotEqual(a[0], self.service.repo.daily_witches(before+1)[0])

    async def setup_witch_room(self):
        host = HashableMember(1, '房主')
        guild = SimpleNamespace(id=1, default_role=object(), me=object(),
                                get_member=lambda uid: host if uid == 1 else None)
        channel = FakeChannel(70, guild)
        channel.delete = AsyncMock()
        self.bot.channels[70] = channel
        self.characters.create(1, 1)
        room = self.service.repo.create(1, 50, 70, 1, WITCH_BOSS, 1)
        room.update(message_id=999, witch_day='2026-09-09', witch_ids=['anan', 'noah', 'meruru'])
        self.service.repo.save(room)
        return room, host, channel

    async def test_free_start_confirm_timeout_reward_exactly_once(self):
        room, host, channel = await self.setup_witch_room()
        with self.store.db:
            add_owned_item(self.store.db, 1, 1, 'proof:raid', 2)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            room = await self.service.begin(room['id'], host)
            self.assertEqual(self.characters.available_quantity(1, 1, 'proof:raid'), 2)
            b = load_total_battle(room['battle'])
            self.assertIsInstance(b, WitchRaidBattle)
            self.assertGreater(room['round_deadline'] - room['created_at'], 119)
            b.living(0)[0].stats['HP'] = b.living(0)[0].hp = 100000
            room['battle'] = dump_total_battle(b)
            self.service.repo.save(room)
            target = b.valid_targets(1, ACTION_ATTACK)[0]
            await self.service.submit_action(room['id'], 1, ACTION_ATTACK, target, None, expected_round=1)
            self.assertEqual(load_total_battle(self.service.repo.get(room['id'])['battle']).round, 0)
            await self.service.confirm_action(room['id'], 1)
            self.assertEqual(load_total_battle(self.service.repo.get(room['id'])['battle']).round, 1)
            with self.assertRaisesRegex(TotalRaidError, '回合已結束'):
                await self.service.submit_action(room['id'], 1, ACTION_ATTACK, target, None, expected_round=1)
            with self.assertRaises(TotalRaidError):
                await self.service.begin(room['id'], host)
            room = self.service.repo.get(room['id'])
            b = load_total_battle(room['battle'])
            b.rewound = True
            for w in b.witches():
                w.hp = 0
            await self.service._resolve(room, b)
            self.service.repo.finish_witch(room, b)
            self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 1)
            self.assertEqual(self.characters.available_quantity(1, 1, 'proof:raid'), 2)
            self.assertFalse(ITEMS['witch:thread'].transferable)
            self.assertLessEqual(len(self.service.battle_embed(room, b)), 6000)

    async def test_no_material_required_to_start(self):
        room, host, channel = await self.setup_witch_room()
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.begin(room['id'], host)
        self.assertEqual(self.service.repo.get(room['id'])['status'], 'running')
        self.assertEqual(self.characters.available_quantity(1, 1, 'proof:raid'), 0)

    async def test_free_group_start_preserves_inventory_and_rejects_duplicate_start(self):
        room, host, channel = await self.setup_witch_room()
        with self.store.db:
            add_owned_item(self.store.db, 1, 1, 'proof:raid', 1)
        room.update(status='running', members=[1, 2])
        self.service.repo.start_witch(room)
        with self.assertRaises(TotalRaidError):
            self.service.repo.start_witch(room)
        self.assertEqual(self.characters.available_quantity(1, 1, 'proof:raid'), 1)
        self.assertEqual(self.service.repo.get(room['id'])['status'], 'running')

    async def test_daily_announcement_not_duplicated_and_cleanup(self):
        room, host, channel = await self.setup_witch_room()
        announcement = FakeChannel(80, channel.guild)
        self.bot.channels[80] = announcement
        self.service.witch_channel_ids = {80}
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.announce_witches()
            await self.service.announce_witches()
            self.assertEqual(announcement.send.await_count, 1)
            await self.service.cleanup_witch_rooms(room['created_at']+1801)
        channel.delete.assert_awaited_once()
        self.assertTrue(self.service.repo.get(room['id'])['channel_deleted'])

    async def test_manual_autoplay_button_clears_draft_and_can_take_back(self):
        room, _, _ = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time() + 120)
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.private_choice(room['id'], 1, 1, 'attack')
            view = WitchPrivateActionView(self.service, room['id'], 1, b)
            self.assertFalse(view.autoplay.disabled)
            self.assertTrue(view.takeover.disabled)
            interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()))
            with patch.object(view, 'refresh', new=AsyncMock()) as refresh:
                await view.autoplay.callback(interaction)
                refresh.assert_awaited_once()
            interaction.followup.send.assert_not_awaited()
            saved = self.service.repo.get(room['id'])
            b = load_total_battle(saved['battle'])
            self.assertEqual(b.auto_players, {1})
            self.assertNotIn('1', saved['action_drafts'])
            self.assertNotIn(1, b.waiting_player_ids())
            view = WitchPrivateActionView(self.service, room['id'], 1, b)
            self.assertTrue(view.autoplay.disabled)
            self.assertFalse(view.takeover.disabled)
            await self.service.takeover_action(room['id'], 1, expected_round=1)
            b = load_total_battle(self.service.repo.get(room['id'])['battle'])
            self.assertNotIn(1, b.auto_players)
            self.assertIn(1, b.waiting_player_ids())
            with self.assertRaisesRegex(TotalRaidError, '回合已結束'):
                await self.service.enable_auto_action(room['id'], 1, expected_round=0)
            with self.assertRaises(TotalRaidError):
                await self.service.enable_auto_action(room['id'], 99, expected_round=1)

    async def test_manual_autoplay_resolves_when_everyone_is_ready(self):
        room, host, _ = await self.setup_witch_room()
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.begin(room['id'], host)
            await self.service.enable_auto_action(room['id'], 1, expected_round=1)
        b = load_total_battle(self.service.repo.get(room['id'])['battle'])
        self.assertEqual(b.round, 1)
        self.assertIn(1, b.auto_players)

    async def test_all_auto_advances_each_tick_until_manual_takeover_after_restart(self):
        room, _, _ = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        for actor in b.living(0):
            actor.hp = actor.stats['HP'] = 1000000
            b.enable_auto(actor.user_id)
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time() + 120)
        self.service.repo.save(room)
        # Read persisted auto mode just as the background loop does after a restart.
        self.service = TotalRaidService(self.cog)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), \
                patch.object(self.service, 'announce_witches', new=AsyncMock()), \
                patch.object(self.service, 'cleanup_witch_rooms', new=AsyncMock()):
            for expected_round in (1, 2):
                await self.service.tick()
                saved = self.service.repo.get(room['id'])
                b = load_total_battle(saved['battle'])
                self.assertEqual(b.round, expected_round)
                self.assertGreater(saved['round_deadline'], time.time())
                self.assertTrue(all(value == 0 for value in b.timeout_streak.values()))
            await self.service.takeover_action(room['id'], 1, expected_round=3)
            await self.service.tick()
            b = load_total_battle(self.service.repo.get(room['id'])['battle'])
            self.assertEqual(b.round, 2)
            self.assertEqual(b.waiting_player_ids(), {1})
            # Once the manual player confirms, the mixed group also advances.
            await self.service.submit_action(room['id'], 1, 'defend', None, None, expected_round=3)
            await self.service.confirm_action(room['id'], 1, expected_round=3)
            b = load_total_battle(self.service.repo.get(room['id'])['battle'])
            self.assertEqual(b.round, 3)

    async def test_private_panel_keeps_each_players_draft_and_validates_round_and_target(self):
        room, host, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time() + 120)
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            view = self.service.view(room)
            self.assertIsInstance(view, WitchBattleView)
            self.assertTrue(view.is_persistent())
            self.assertTrue(any(c.custom_id == 'total_raid:running:action' for c in view.children))
            await self.service.private_choice(room['id'], 1, 1, 'attack')
            await self.service.private_choice(room['id'], 2, 1, 'attack')
            target = b.valid_targets(1, ACTION_ATTACK)[0]
            with self.assertRaisesRegex(TotalRaidError, '有效'):
                await self.service.private_choice(room['id'], 1, 1, 'p:2', True)
            with self.assertRaisesRegex(TotalRaidError, '先選擇目標'):
                await self.service.confirm_action(room['id'], 1)
            await self.service.private_choice(room['id'], 1, 1, target, True)
            await self.service.confirm_action(room['id'], 1)
            updated = self.service.repo.get(room['id'])
            self.assertIn('2', updated['action_drafts'])
            self.assertNotIn('1', updated['action_drafts'])
            self.assertEqual(load_total_battle(updated['battle']).confirmed, {1})
            await self.service.private_choice(room['id'], 1, 1, 'defend')
            self.assertFalse(load_total_battle(self.service.repo.get(room['id'])['battle']).confirmed)
            with self.assertRaisesRegex(TotalRaidError, '回合已更新'):
                await self.service.private_choice(room['id'], 1, 0, target, True)

    async def test_all_summary_and_status_pages_fit_discord_and_preserve_text(self):
        room, _, _ = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make(ids=('ema', 'sherry', 'hanna'))
        for p in b.living(0):
            p.name = f'玩家{p.user_id}' + '很長的暱稱' * 6
            p.passive_id = 1
            for effect in ('factor', 'burn', 'vision', 'doubt', 'watch', 'exchange', 'taunt', 'guard', 'bless', 'stun',
                           'brainwash', 'no_look', 'defend', 'stance', 'moon_shadow', 'break', 'poison', 'weak', 'vulnerable'):
                p.effects[effect] = 2
            p.status_stacks['factor'] = 3
        for w in b.witches():
            b.prepare(w)
        for _ in range(24):
            b.add_object(b.witch('hanna'), 'rock', .04)
        b.mechanics['last_round_log'] = [f'紀錄 {i}：' + '完整戰鬥摘要' * 12 for i in range(90)] + ['長行' * 1500]
        room.update(status='running', round_deadline=time.time()+120)
        expected = field_chunks(b.mechanics['last_round_log'], 900)
        observed = []
        for page in range(len(expected)):
            room['log_page'] = page
            embed = self.service.battle_embed(room, b)
            self.assertLessEqual(len(embed), 6000)
            self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))
            observed.extend(f.value for f in embed.fields if f.name.startswith('上一回合摘要'))
        self.assertEqual(observed, expected)
        self.assertEqual(''.join(observed).replace('\n', ''), ''.join(b.mechanics['last_round_log']))
        self.assertGreater(room['status_page_count'], 1)
        all_fields = []
        for page in range(room['status_page_count']):
            room['status_page'] = page
            embed = self.service.battle_embed(room, b)
            self.assertLessEqual(len(embed), 6000)
            all_fields.extend(f.value for f in embed.fields)
        text = '\n'.join(all_fields)
        self.assertIn('魔女因子 3/3 層', text)
        self.assertIn('**火傷**：回合結束扣除最大 HP 的 3%', text)
        self.assertIn('技能', text)
        self.assertEqual('**魔女化（0 階）**', effect_status(b.witch('ema'), b)[0])
        self.assertIn('引爆最多魔女因子', text)
        self.assertIn('**魔女因子 3/3 層**', text)

    async def test_private_buttons_target_confirmation_cooldown_and_stale_panel(self):
        room, _, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        b._player(1).ready[1] = 3
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time()+120)
        self.service.repo.save(room)
        interaction = SimpleNamespace(user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            public = self.service.view(room)
            self.assertFalse(any(isinstance(c, discord.ui.Select) for c in public.children))
            self.assertNotIn(public.confirm, public.children)
            private = WitchPrivateActionView(self.service, room['id'], 1, b)
            skill = next(c for c in private.children if isinstance(c, WitchActionButton) and c.action.get('skill_slot') == 1)
            self.assertTrue(skill.disabled)
            self.assertIn('CD 2', skill.label)
            attack = next(c for c in private.children if isinstance(c, WitchActionButton) and c.action['action'] == ACTION_ATTACK)
            await attack.callback(interaction)
            updated = interaction.edit_original_response.call_args.kwargs['view']
            target = next(c for c in updated.children if isinstance(c, WitchPrivateTargetSelect))
            target._values = [b.valid_targets(1, ACTION_ATTACK)[0]]
            await target.callback(interaction)
            updated = interaction.edit_original_response.call_args.kwargs['view']
            self.assertFalse(updated.confirm.disabled)
            self.assertIn('目標：', interaction.edit_original_response.call_args.kwargs['content'])
            await updated.confirm.callback(interaction)
            self.assertEqual(load_total_battle(self.service.repo.get(room['id'])['battle']).confirmed, {1})
            self.assertIn('已確認', interaction.edit_original_response.call_args.kwargs['content'])
            with self.assertRaisesRegex(TotalRaidError, '回合已結束'):
                await self.service.confirm_action(room['id'], 1, expected_round=0)
            await self.service.private_choice(room['id'], 1, 1, 'attack')
            with self.assertRaisesRegex(TotalRaidError, '另一個面板修改'):
                await self.service.private_choice(room['id'], 1, 1, target._values[0], True,
                    expected_action=dict(action='skill', slot=2, round=1))
            interaction.user.id = 2
            self.assertFalse(await updated.interaction_check(interaction))
        b = battle_fixtures.WitchBattleTests().make(phase=2)
        b.prepare(b.witch('anan'))
        user = b.fighter_for_key(next(iter(b.commands))).user_id
        room.update(battle=dump_total_battle(b), action_drafts={})
        self.service.repo.save(room)
        private = WitchPrivateActionView(self.service, room['id'], user, b)
        buttons = [c for c in private.children if isinstance(c, WitchActionButton)]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].action['action'], ACTION_ATTACK)

    async def test_open_private_panel_creates_separate_response_and_buttons_refresh_it(self):
        room, _, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time()+120)
        self.service.repo.save(room)
        interaction = SimpleNamespace(user=SimpleNamespace(id=1),
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      edit_original_response=AsyncMock())
        public = WitchBattleView(self.service, room['id'])
        await public.choose.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        channel.message.edit.assert_not_awaited()
        private = interaction.edit_original_response.call_args.kwargs['view']
        self.assertIsInstance(private, WitchPrivateActionView)
        click = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        attack = next(item for item in private.children
                      if isinstance(item, WitchActionButton) and item.action['action'] == ACTION_ATTACK)
        await attack.callback(click)
        # In-panel components edit their existing private message, without a new response.
        click.response.defer.assert_awaited_once_with()
        refreshed = click.edit_original_response.call_args.kwargs['view']
        self.assertTrue(any(isinstance(item, WitchPrivateTargetSelect) for item in refreshed.children))
        self.assertIs(self.service.private_panels[(room['id'], 1)]['interaction'], click)
        self.assertTrue(private.is_finished())

    async def test_round_resolution_refreshes_all_private_panels_and_clears_finished_views(self):
        room, _, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        for player in b.living(0):
            player.hp = player.stats['HP'] = 100000
        b._player(1).ready[1] = 3
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)),
                    round_deadline=time.time()+120)
        self.service.repo.save(room)
        panels = [SimpleNamespace(edit_original_response=AsyncMock()) for _ in range(2)]
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            for uid, interaction in enumerate(panels, 1):
                await self.service.refresh_private_panel(room, uid, interaction)
            old_view = self.service.private_panels[(room['id'], 1)]['view']
            await self.service._resolve(room, b, timeout=True)
            for interaction in panels:
                self.assertEqual(interaction.edit_original_response.await_count, 2)
                args = interaction.edit_original_response.call_args.kwargs
                self.assertIn('第 2 回合', args['content'])
                self.assertEqual(args['view'].planning_round, 2)
                self.assertTrue(args['view'].confirm.disabled)
            self.assertTrue(old_view.is_finished())
            # One vanished private message must not block the other player's update.
            panels[0].edit_original_response.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Missing'), 'gone')
            await self.service._resolve(room, b, timeout=True)
            self.assertNotIn((room['id'], 1), self.service.private_panels)
            self.assertIn('第 3 回合', panels[1].edit_original_response.call_args.kwargs['content'])
            b.result = '戰敗'
            await self.service._resolve(room, b)
            self.assertIn('戰鬥已結束', panels[1].edit_original_response.call_args.kwargs['content'])
            self.assertIsNone(panels[1].edit_original_response.call_args.kwargs['view'])
            self.assertFalse(self.service.private_panels)

    async def test_private_refresh_runs_even_if_public_update_fails_and_retries_latest_state(self):
        room, _, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)), round_deadline=time.time()+120)
        self.service.repo.save(room)
        interaction = SimpleNamespace(edit_original_response=AsyncMock())
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), patch('core.rpg_total_raids.logger.exception'):
            await self.service.refresh_private_panel(room, 1, interaction)
            failure = discord.HTTPException(SimpleNamespace(status=500, reason='Server Error'), 'retry')
            channel.message.edit.side_effect = failure
            with self.assertRaises(discord.HTTPException):
                await self.service._resolve(room, b, timeout=True)
            self.assertIn('第 2 回合', interaction.edit_original_response.call_args.kwargs['content'])
            interaction.edit_original_response.side_effect = failure
            await self.service.refresh_private_panels(room)
            self.assertIn('retry_at', self.service.private_panels[(room['id'], 1)])
            interaction.edit_original_response.side_effect = None
            await self.service.refresh_private_panels(self.service.repo.get(room['id']))
            self.assertNotIn('retry_at', self.service.private_panels[(room['id'], 1)])

    async def test_surrender_requires_all_living_votes_supports_revoke_and_no_rewards(self):
        room, _, channel = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        for player in b.living(0)[2:]:
            player.hp = 0
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)), round_deadline=time.time()+120)
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.vote_surrender(room['id'], 1, 1)
            self.assertEqual(self.service.repo.get(room['id'])['surrender_votes'], [1])
            await self.service.vote_surrender(room['id'], 1, 1)
            self.assertEqual(self.service.repo.get(room['id'])['surrender_votes'], [])
            with self.assertRaises(TotalRaidError):
                await self.service.vote_surrender(room['id'], 3, 1)
            with self.assertRaisesRegex(TotalRaidError, '過期'):
                await self.service.vote_surrender(room['id'], 1, 0)
            await self.service.vote_surrender(room['id'], 1, 1)
            self.service = TotalRaidService(self.cog)
            await self.service.vote_surrender(room['id'], 2, 1)
        saved = self.service.repo.get(room['id'])
        ended = load_total_battle(saved['battle'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(ended.result, '投降')
        self.assertEqual(ended.round, 0)
        self.assertIsNone(saved['round_deadline'])
        self.assertIn('投降', ended.mechanics['witch_round_logs'][-1][0])
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 0)
        self.assertFalse(self.characters.unlocked_witch_embroideries(1, 1))

    async def test_partial_surrender_vote_expires_on_round_resolution(self):
        room, _, _ = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make()
        room.update(status='running', battle=dump_total_battle(b), members=list(range(1, 7)), round_deadline=time.time()+120)
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.vote_surrender(room['id'], 1, 1)
            room = self.service.repo.get(room['id'])
            await self.service._resolve(room, b, timeout=True)
        self.assertEqual(self.service.repo.get(room['id'])['surrender_votes'], [])

    async def test_archive_retries_before_deleting_and_does_not_duplicate_on_delete_retry(self):
        room, _, channel = await self.setup_witch_room()
        announcement = FakeChannel(80, channel.guild)
        self.bot.channels[80] = announcement
        self.service.witch_channel_ids = {80}
        b = battle_fixtures.WitchBattleTests().make()
        b.result = '勝利'
        b.mechanics['witch_round_logs'] = [['第一回合完整紀錄'], ['最後一回合完整紀錄']]
        room.update(status='completed', battle=dump_total_battle(b), finished_at=time.time())
        self.service.repo.save(room)
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'missing permission')
        announcement.send.side_effect = forbidden
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), patch('core.rpg_total_raids.logger.exception'):
            await self.service.cleanup_witch_rooms(time.time())
            channel.delete.assert_not_awaited()
            announcement.send.side_effect = None
            channel.delete.side_effect = forbidden
            await self.service.cleanup_witch_rooms(time.time())
            self.assertTrue(self.service.repo.get(room['id'])['archive_message_id'])
            file = announcement.send.call_args.kwargs['file']
            contents = file.fp.getvalue().decode('utf-8-sig')
            self.assertIn('第一回合完整紀錄', contents)
            self.assertIn('最後一回合完整紀錄', contents)
            channel.delete.side_effect = None
            await self.service.cleanup_witch_rooms(time.time())
            self.assertEqual(announcement.send.await_count, 2)
            self.assertTrue(self.service.repo.get(room['id'])['channel_deleted'])

    async def test_announcement_refreshes_existing_message_with_abilities_and_free_entry(self):
        _, channel = self.announcement_fixture(existing=True)
        self.bot.channels[80] = channel
        self.service.witch_channel_ids = {80}
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_witch_announcements VALUES (?,?,?)', (80, witch_day(), 999))
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            await self.service.announce_witches()
            await self.service.announce_witches()
        channel.send.assert_not_awaited()
        channel.message.edit.assert_awaited_once()
        embed = channel.message.edit.call_args.kwargs['embed']
        self.assertIn('免費入場', embed.description)
        self.assertEqual(len(embed.fields), 3)
        self.assertTrue(all('二次魔女化' in f.value for f in embed.fields))
        channel.message.pin.assert_awaited_once()

    async def test_daily_pin_rotation_retries_without_reposting_or_losing_old_pin(self):
        _, channel = self.announcement_fixture(existing=True)
        self.bot.channels[80] = channel
        self.service.witch_channel_ids = {80}
        old = SimpleNamespace(id=998, pin=AsyncMock(), unpin=AsyncMock())
        channel.get_partial_message = lambda mid: old if mid == 998 else channel.message
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_witch_announcements VALUES (?,?,?)', (80, '2000-01-01', 998))
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no pin permission')
        channel.message.pin.side_effect = forbidden
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel), patch('core.rpg_total_raids.logger.exception'):
            await self.service.announce_witches()
            await self.service.announce_witches()
            channel.send.assert_awaited_once()
            channel.message.pin.assert_awaited_once()
            old.unpin.assert_not_awaited()
            channel.message.pin.side_effect = None
            old.unpin.side_effect = forbidden
            self.service.witch_pin_retry_at.clear()
            await self.service.announce_witches()
            self.assertEqual(self.service.pinned_announcements[80], 999)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rpg_witch_unpin_queue').fetchone()[0], 1)
            old.unpin.side_effect = None
            restarted = TotalRaidService(self.cog)
            restarted.witch_channel_ids = {80}
            await restarted.announce_witches()
            channel.send.assert_awaited_once()
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rpg_witch_unpin_queue').fetchone()[0], 0)

    async def test_embroidery_consumption_and_snapshot(self):
        room, host, channel = await self.setup_witch_room()
        accessory = next(k for k, item in ITEMS.items() if item.slot == '飾品' and item.embroidery_slots and not item.job)
        with self.store.db:
            add_owned_item(self.store.db, 1, 1, accessory)
            add_owned_item(self.store.db, 1, 1, 'witch:thread', 3)
        instance = next(i for i in self.characters.equipment_instances(1, 1) if i.item_id == accessory)
        with self.assertRaises(Exception):
            self.characters.embroider_accessory(1, 1, instance.token, 'witch_dawn')
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 3)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO rpg_wallets(guild_id,user_id,gold) VALUES (1,1,1000)')
        with self.assertRaisesRegex(Exception, '尚未解鎖'):
            self.characters.embroider_accessory(1, 1, instance.token, 'witch_dawn')
        self.assertEqual(self.store.gold(1, 1), 1000)
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 3)
        with self.store.db:
            record_victory(self.store.db, 1, [1], ['hiro'], 'old-win')
        self.characters.embroider_accessory(1, 1, instance.token, 'witch_dawn')
        self.assertEqual(self.characters.available_quantity(1, 1, 'witch:thread'), 0)
        saved = self.characters._instance(1, 1, instance.instance_id)
        self.assertTrue(any(a[1] == 'embroidery:witch_dawn' for a in saved.affixes))

    async def test_win_unlocks_all_participants_only_and_backfills_existing_wins(self):
        room, _, _ = await self.setup_witch_room()
        b = battle_fixtures.WitchBattleTests().make(ids=('hiro', 'margo', 'anan'))
        room.update(status='completed', members=[1, 2], witch_ids=list(b.ids))
        b.result = '戰敗'
        room['battle'] = dump_total_battle(b)
        self.service.repo.finish_witch(room, b)
        self.assertFalse(self.characters.unlocked_witch_embroideries(1, 1))
        # Historical victory before the unlock migration, including a dead participant.
        b.result = '勝利'
        b._player(2).hp = 0
        room['battle'] = dump_total_battle(b)
        self.service.repo.save(room)
        with self.store.db:
            self.store.db.execute('DELETE FROM rpg_witch_unlock_migrations')
        TotalRaidStore(self.store)
        expected = {'witch_dawn', 'witch_echo', 'witch_wish'}
        self.assertEqual(self.characters.unlocked_witch_embroideries(1, 1), expected)
        self.assertEqual(self.characters.unlocked_witch_embroideries(1, 2), expected)
        self.assertFalse(self.characters.unlocked_witch_embroideries(1, 3))
        self.assertFalse(self.characters.unlocked_witch_embroideries(2, 1))
        self.service.repo.finish_witch(room, b)
        TotalRaidStore(self.store)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rpg_witch_unlocks').fetchone()[0], 6)
        self.assertEqual(len(REQUIREMENTS), 13)
