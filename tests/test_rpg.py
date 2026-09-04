from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import MAX_LEVEL, RPGStore, VoiceTracker, eligible_voice_members, level_for, level_floor
from core.settings import SettingsError, load_settings


class RPGTests(unittest.TestCase):
    def test_level_boundaries(self):
        for level in range(1, MAX_LEVEL):
            floor = level_floor(level)
            self.assertEqual(level_for(floor), level)
            self.assertEqual(level_for(level_floor(level + 1) - 1), level)

    def test_runescape_reference_thresholds_and_cap(self):
        reference = {1: 0, 2: 83, 3: 174, 5: 388, 10: 1154, 20: 4470,
                     30: 13363, 50: 101333, 70: 737627, 92: 6517253,
                     99: 13034431, 100: 14391160, 120: 104273167}
        for level, xp in reference.items():
            self.assertEqual(level_floor(level), xp)
            self.assertEqual(level_for(xp), level)
        self.assertEqual(level_for(200000000), MAX_LEVEL)
        for level in (0, MAX_LEVEL + 1):
            with self.assertRaises(ValueError):
                level_floor(level)
        with self.assertRaises(ValueError):
            level_for(-1)

    def test_persistent_cooldown_guild_isolation_and_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rpg.db'
            store = RPGStore(path)
            store.award_text(1, 10, 1000, 15, 60)
            store.award_text(1, 10, 1059, 15, 60)
            self.assertEqual(store.xp(1, 10), 15)
            store.close()
            store = RPGStore(path)
            try:
                store.award_text(1, 10, 1059, 15, 60)
                self.assertEqual(store.xp(1, 10), 15)
                store.award_text(1, 10, 1060, 15, 60)
                store.award_text(2, 10, 1060, 15, 60)
                store.award_voice([(1, 10, 10), (1, 20, 60)])
                self.assertEqual(store.xp(1, 10), 40)
                self.assertEqual(store.xp(2, 10), 15)
                self.assertEqual(store.leaders(1), [(20, 60), (10, 40)])
                # Voice-created players must receive their first text award.
                store.award_text(1, 20, 1060, 15, 60)
                self.assertEqual(store.xp(1, 20), 75)
                self.assertEqual(store.xp(3, 10), 0)
            finally:
                store.close()

    def test_voice_minutes_transitions_and_disconnect(self):
        tracker = VoiceTracker()
        self.assertEqual(tracker.update(1, {10, 20}, 0, 10), [])
        self.assertEqual(tracker.update(1, {10, 20}, 59, 10), [])
        self.assertEqual(tracker.update(1, {10, 20}, 60, 10), [(1, 10, 10), (1, 20, 10)])
        self.assertEqual(tracker.update(1, set(), 90, 10), [])
        tracker.update(1, {10}, 100, 10)
        self.assertEqual(tracker.update(1, {10}, 159, 10), [])
        self.assertEqual(tracker.update(1, set(), 160, 10), [(1, 10, 10)])
        tracker.update(1, {10}, 200, 10)
        tracker.clear()
        self.assertEqual(tracker.update(1, {10}, 1000, 10), [])
        self.assertEqual(tracker.update(2, {10}, 1000, 10), [])

    def test_voice_eligibility(self):
        def member(user_id, bot=False, **flags):
            state = dict(self_mute=False, mute=False, self_deaf=False, deaf=False, suppress=False)
            state.update(flags)
            return SimpleNamespace(id=user_id, bot=bot, voice=SimpleNamespace(**state))
        first, second = member(1), member(2)
        channel = SimpleNamespace(id=1, members=[first, second, member(3, bot=True)])
        afk = SimpleNamespace(id=2, members=[member(4), member(5)])
        guild = SimpleNamespace(voice_channels=[channel, afk], afk_channel=afk)
        self.assertEqual(eligible_voice_members(guild, 2), {1, 2})
        for flag in ('self_mute', 'mute', 'self_deaf', 'deaf', 'suppress'):
            setattr(second.voice, flag, True)
            self.assertEqual(eligible_voice_members(guild, 2), set())
            setattr(second.voice, flag, False)

    def test_settings_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.toml'
            for value in ('voice_min_members = 1', 'text_xp = -1', 'enabled = 1'):
                path.write_text('[rpg]\n' + value, encoding='utf-8')
                with self.assertRaises(SettingsError):
                    load_settings(path)


class RPGIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_lifecycle_and_message_filters(self):
        import discord
        from discord.ext import commands

        with tempfile.TemporaryDirectory() as directory:
            store = RPGStore(Path(directory) / 'rpg.db')
            async with commands.Bot(command_prefix=[], intents=discord.Intents.none()) as bot:
                with patch('core.rpg.RPGStore', return_value=store):
                    await bot.load_extension('cmds.rpg')
                cog = bot.get_cog('RPG')
                self.assertIsNotNone(cog)
                self.assertEqual({command.name for command in bot.tree.get_commands()},
                                 {'冒險', '排行榜', '生成討伐', '討伐通知'})
                admin = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2),
                    permissions=SimpleNamespace(administrator=False),
                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()))
                with patch.object(cog.raids, 'spawn', new_callable=AsyncMock) as spawn:
                    await cog.spawn_raid.callback(cog, admin)
                    spawn.assert_not_awaited()
                    admin.permissions.administrator = True
                    spawn.return_value = SimpleNamespace(jump_url='https://discord.com/channels/1/2/3')
                    await cog.spawn_raid.callback(cog, admin)
                    spawn.assert_awaited_once_with(admin.channel, kind=None, name=None, strength=1.0,
                                                   victory_xp=None, victory_gold=None, drop_percent=None)
                    admin.response.defer.assert_awaited_once_with(ephemeral=True)
                    self.assertIn('五分鐘', admin.followup.send.call_args.args[0])
                self.assertTrue(cog.spawn_raid.default_permissions.administrator)
                def message(**overrides):
                    values = dict(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=10, bot=False),
                                  webhook_id=None, content='今天一起冒險', is_system=lambda: False)
                    values.update(overrides)
                    return SimpleNamespace(**values)
                for invalid in (message(guild=None), message(author=SimpleNamespace(id=10, bot=True)),
                                message(webhook_id=1), message(content=' a b '),
                                message(content=''), message(is_system=lambda: True)):
                    await cog.on_message(invalid)
                self.assertEqual(store.xp(1, 10), 0)
                with patch('cmds.rpg.time.time', return_value=1000):
                    await cog.on_message(message())
                    await cog.on_message(message())
                self.assertEqual(store.xp(1, 10), 15)
                user = SimpleNamespace(id=10, display_name='測試冒險者',
                                       display_avatar=SimpleNamespace(url='https://example.com/avatar.png'))
                interaction = SimpleNamespace(user=user, guild_id=1,
                                              response=SimpleNamespace(send_message=AsyncMock()))
                interaction.response.edit_message = AsyncMock()
                interaction.edit_original_response = AsyncMock()
                await cog.adventure.callback(cog, interaction)
                embed = interaction.response.send_message.call_args.kwargs['embed']
                self.assertIn('安安大冒險', embed.title)
                self.assertIn('15 / 83 XP', embed.fields[0].value)
                self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
                home = interaction.response.send_message.call_args.kwargs['view']
                await home.handle(interaction, 'jobs')
                jobs = interaction.response.edit_message.call_args.kwargs['view']
                self.assertTrue(home.is_finished())
                await jobs.handle(interaction, 'job', '騎士')
                await jobs.handle(interaction, 'change_job')
                self.assertIn('Lv.10', interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
                store.award_voice([(1, 10, 200000000)])
                await jobs.handle(interaction, 'change_job')
                self.assertIn('精銳騎士', interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
                await jobs.handle(interaction, 'home')
                home = interaction.response.edit_message.call_args.kwargs['view']
                self.assertIn('Lv.120', home.embed().title)
                await home.handle(interaction, 'skills')
                skills = interaction.response.edit_message.call_args.kwargs['view']
                await skills.handle(interaction, 'slot', '2')
                await skills.handle(interaction, 'priority', '1')
                await skills.handle(interaction, 'toggle')
                rules = cog.tactics.rules(1, 10, '騎士')
                self.assertEqual((rules[0].slot, rules[0].enabled), (2, False))
                await skills.handle(interaction, 'home')
                home = interaction.response.edit_message.call_args.kwargs['view']
                await home.handle(interaction, 'equipment')
                panel = interaction.response.edit_message.call_args.kwargs['view']
                await panel.handle(interaction, 'slot', '飾品5')
                await panel.handle(interaction, 'item', 'accessory:0')
                before = cog.characters.snapshot(1, 10)['total'][0]
                await panel.handle(interaction, 'wear')
                self.assertEqual(cog.characters.snapshot(1, 10)['total'][0], before + 3)
                await panel.handle(interaction, 'remove')
                self.assertEqual(cog.characters.snapshot(1, 10)['total'][0], before)
                await panel.handle(interaction, 'home')
                home = interaction.response.edit_message.call_args.kwargs['view']
                await home.handle(interaction, 'backpack')
                backpack = interaction.response.edit_message.call_args.kwargs['view']
                self.assertIn('背包 1/1', backpack.embed().title)
                await backpack.handle(interaction, 'give')
                gift = interaction.response.edit_message.call_args.kwargs['view']
                self.assertEqual(gift.mode, 'give')
                await gift.handle(interaction, 'back')
                backpack = interaction.response.edit_message.call_args.kwargs['view']
                await backpack.handle(interaction, 'home')
                home = interaction.response.edit_message.call_args.kwargs['view']
                await home.handle(interaction, 'shop')
                shop = interaction.response.edit_message.call_args.kwargs['view']
                await shop.handle(interaction, 'sell')
                selling = interaction.response.edit_message.call_args.kwargs['view']
                self.assertEqual(selling.mode, 'sell')
                await selling.handle(interaction, 'back')
                shop = interaction.response.edit_message.call_args.kwargs['view']
                await shop.handle(interaction, 'home')
                home = interaction.response.edit_message.call_args.kwargs['view']
                await home.handle(interaction, 'help')
                help_view = interaction.response.edit_message.call_args.kwargs['view']
                self.assertIn('RuneScape', help_view.embed().description)
                await help_view.handle(interaction, 'close')
                self.assertTrue(help_view.is_finished())
                cog.tracker.update(1, {10}, 0, 10)
                await cog.on_disconnect()
                self.assertEqual(cog.tracker.sessions, {})
                await bot.unload_extension('cmds.rpg')
                self.assertEqual(bot.tree.get_commands(), [])


if __name__ == '__main__':
    unittest.main()
