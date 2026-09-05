from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore
from core.rpg_battle import Tactics
from core.rpg_character import CharacterError, Characters
from core.rpg_total_battle import ACTION_ATTACK, ACTION_SKILL, load_total_battle
from core.rpg_total_raids import (
    TotalRaidError, TotalRaidService, TotalRaidStore, effect_status, hp_bar,
)
from core.settings import RPGSettings


class HashableMember:
    def __init__(self, user_id, name='玩家', bot=False):
        self.id, self.display_name, self.bot = user_id, name, bot

    def __str__(self):
        return self.display_name


class FakeCategory:
    def __init__(self, category_id, channel=None):
        self.id, self.channel = category_id, channel
        self.create_text_channel = AsyncMock(return_value=channel)


class FakeChannel:
    def __init__(self, channel_id, guild):
        self.id, self.guild = channel_id, guild
        self.mention = f'<#{channel_id}>'
        self.message = SimpleNamespace(id=999, edit=AsyncMock())
        self.send = AsyncMock(return_value=self.message)

    def get_partial_message(self, _message_id):
        return self.message


class FakeBot:
    def __init__(self):
        self.channels = {}
        self.add_view = unittest.mock.Mock()

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def is_ready(self):
        return True


class TotalRaidRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.tactics = Tactics(self.store)
        self.bot = FakeBot()
        self.cog = SimpleNamespace(
            bot=self.bot, store=self.store, settings=self.settings,
            characters=self.characters, tactics=self.tactics,
        )
        with patch.dict('os.environ', {'RPG_TOTAL_RAID_CATEGORY_IDS': '50'}):
            self.service = TotalRaidService(self.cog)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_numbering_is_persistent_per_boss(self):
        repo = TotalRaidStore(self.store)
        self.assertEqual(repo.reserve_number(1, '訓練用假人'), 1)
        self.assertEqual(repo.reserve_number(1, '訓練用假人'), 2)
        self.assertEqual(repo.reserve_number(2, '訓練用假人'), 1)

    async def test_admin_room_creation_names_channel_and_auto_joins_host(self):
        host = HashableMember(1, '房主')
        guild = SimpleNamespace(id=10, default_role=object(), me=object())
        channel = FakeChannel(70, guild)
        category = FakeCategory(50, channel)
        guild.get_channel = lambda channel_id: category if channel_id == 50 else None
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory):
            room, created = await self.service.create_room(guild, host, '訓練用假人')
        self.assertIs(created, channel)
        self.assertEqual(room['members'], [1])
        kwargs = category.create_text_channel.call_args.kwargs
        self.assertEqual(kwargs['name'], '總力戰-訓練用假人-1')
        self.assertIn(host, kwargs['overwrites'])
        channel.send.assert_awaited_once()
        self.assertEqual(self.service.repo.get(room['id'])['message_id'], 999)
        with patch('core.rpg_total_raids.discord.CategoryChannel', FakeCategory):
            with self.assertRaisesRegex(CharacterError, '另一個總力戰'):
                await self.service.create_room(guild, host, '訓練用假人')

    async def test_lobby_membership_host_rule_capacity_and_other_room(self):
        first = self.service.repo.create(1, 50, 70, 1, '訓練用假人', 1)
        with self.assertRaisesRegex(TotalRaidError, '房主不能退出'):
            await self.service.change_member(first['id'], HashableMember(1), leave=True)
        for user_id in range(2, 7):
            await self.service.change_member(first['id'], HashableMember(user_id))
        with self.assertRaisesRegex(TotalRaidError, '隊伍已滿'):
            await self.service.change_member(first['id'], HashableMember(7))
        second = self.service.repo.create(1, 50, 71, 10, '訓練用假人', 2)
        with self.assertRaisesRegex(TotalRaidError, '另一個總力戰'):
            await self.service.change_member(second['id'], HashableMember(2))

    async def test_only_host_starts_and_single_player_can_submit_round(self):
        host = HashableMember(1, '房主')
        guild = SimpleNamespace(id=1, get_member=lambda uid: host if uid == 1 else None)
        channel = FakeChannel(70, guild)
        self.bot.channels[70] = channel
        room = self.service.repo.create(1, 50, 70, 1, '訓練用假人', 1)
        room['message_id'] = 999
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            with self.assertRaisesRegex(TotalRaidError, '只有開房'):
                await self.service.begin(room['id'], HashableMember(2))
            started = await self.service.begin(room['id'], host)
            battle = load_total_battle(started['battle'])
            embed = self.service.battle_embed(started, battle)
            self.assertEqual([field.name for field in embed.fields[:4]],
                             ['Boss HP', 'Boss 行動', '隊伍狀態', '上一回合完整摘要'])
            self.assertIn('████████████████', embed.fields[0].value)
            action_text = self.service.player_action_text(battle, 1)
            self.assertIn('技能冷卻', action_text)
            self.assertEqual(action_text.count('可使用'), 3)
            target = battle.key(battle.living(1)[0])
            battle.submit(1, ACTION_SKILL, target, 1)
            battle.resolve()
            self.assertIn('槽 1【奮力一擊】：CD 2 回合',
                          self.service.player_action_text(battle, 1))
            await self.service.submit_action(room['id'], 1, ACTION_ATTACK, target, None)
        saved = self.service.repo.get(room['id'])
        self.assertEqual(load_total_battle(saved['battle']).round, 1)
        self.assertEqual(saved['status'], 'running')
        self.assertGreater(saved['round_deadline'], started['round_deadline'] - 1)

    async def test_lobby_embed_shows_six_person_limit(self):
        room = self.service.repo.create(1, 50, 70, 1, '訓練用假人', 1)
        embed = self.service.lobby_embed(room)
        self.assertEqual(embed.fields[1].value, '1/6')
        self.assertIn('不發放獎勵', embed.description)

    async def test_only_host_can_close_abandoned_lobby(self):
        room = self.service.repo.create(1, 50, 70, 1, '訓練用假人', 1)
        with self.assertRaisesRegex(TotalRaidError, '只有開房'):
            await self.service.cancel_lobby(room['id'], HashableMember(2))
        await self.service.cancel_lobby(room['id'], HashableMember(1))
        self.assertEqual(self.service.repo.get(room['id'])['status'], 'cancelled')
        self.assertEqual(self.service.repo.active(), [])

    async def test_hp_bar_and_private_confirmation_cleanup(self):
        self.assertEqual(hp_bar(50, 100, 4), '`██░░` 50.0%')
        self.assertEqual(hp_bar(1, 100, 4), '`█░░░` 1.0%')
        interaction = SimpleNamespace(delete_original_response=AsyncMock())
        with patch('core.rpg_total_raids.asyncio.sleep', new=AsyncMock()):
            await self.service._delete_private_response(interaction, 8)
        interaction.delete_original_response.assert_awaited_once()

    async def test_team_status_lists_active_buffs_debuffs_and_full_round_log(self):
        host = HashableMember(1, '房主')
        guild = SimpleNamespace(id=1, get_member=lambda uid: host if uid == 1 else None)
        channel = FakeChannel(70, guild)
        self.bot.channels[70] = channel
        room = self.service.repo.create(1, 50, 70, 1, '訓練用假人', 1)
        room['message_id'] = 999
        self.service.repo.save(room)
        with patch('core.rpg_total_raids.discord.TextChannel', FakeChannel):
            room = await self.service.begin(room['id'], host)
        battle = load_total_battle(room['battle'])
        fighter = battle.fighters[0]
        boss = battle.fighters[-1]
        fighter.effects.update(guard=1, poison=2)
        fighter.guard_bonus = 15
        fighter.status_stacks['corruption'] = 2
        boss.effects.update({'stance': 1, 'break': 1})
        buffs, debuffs = effect_status(fighter, battle)
        self.assertIn('護衛(防禦+15・1回合)', buffs)
        self.assertIn('中毒(2回合)', debuffs)
        self.assertIn('腐敗(2/3層)', debuffs)
        complete = [f'完整紀錄 {index}：' + '測' * 80 for index in range(20)]
        battle.mechanics['last_round_log'] = complete
        embed = self.service.battle_embed(room, battle)
        boss_status = next(field.value for field in embed.fields if field.name == 'Boss HP')
        self.assertIn('Buff：防禦姿態(減傷35%・1回合)', boss_status)
        self.assertIn('Debuff：破甲(防禦-40%・1回合)', boss_status)
        team = next(field.value for field in embed.fields if field.name == '隊伍狀態')
        self.assertIn('Buff：護衛', team)
        self.assertIn('Debuff：中毒', team)
        log_fields = [field for field in embed.fields if field.name.startswith('上一回合完整摘要')]
        self.assertGreater(len(log_fields), 1)
        self.assertTrue(all(len(field.value) <= 1024 for field in log_fields))
        rebuilt = '\n'.join(field.value for field in log_fields)
        self.assertEqual(rebuilt, '\n'.join(complete))


if __name__ == '__main__':
    unittest.main()
