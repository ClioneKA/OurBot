import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from ourbot import OurBot, sync_guild_ids


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = OurBot()
        self.addAsyncCleanup(self.bot.close)
        async def no_guilds(**kwargs):
            for guild in ():
                yield guild
        fetcher = patch.object(self.bot, 'fetch_guilds', side_effect=no_guilds)
        self.fetch_guilds = fetcher.start()
        self.addCleanup(fetcher.stop)
        verifier = patch.object(self.bot, 'verify_global_commands', new_callable=AsyncMock)
        self.verifier = verifier.start()
        self.addCleanup(verifier.stop)

    async def install_command(self, name):
        if not self.bot.tree.get_commands():
            async def ping(interaction):
                pass
            self.bot.tree.add_command(discord.app_commands.Command(
                name='ping', description='Startup test', callback=ping))

    async def test_loads_all_extensions_before_global_sync(self):
        async def sync(**kwargs):
            self.assertEqual(loader.await_count, 3)
            self.assertIsNone(kwargs['guild'])
            return [SimpleNamespace(name='ping', id=123)]
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command) as loader, \
                patch.object(self.bot.tree, 'sync', side_effect=sync) as synchronizer:
            await self.bot.setup_hook()
            await self.bot.on_ready()
            await self.bot.on_ready()
        self.assertEqual([call.args[0] for call in loader.await_args_list],
                         ['cmds.ai', 'cmds.anan', 'cmds.rpg'])
        synchronizer.assert_awaited_once()

    async def test_guild_sync_copies_commands_without_global_sync(self):
        self.bot.sync_guild_ids = (123, 456)
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command), \
                patch.object(self.bot.tree, 'sync', new_callable=AsyncMock) as sync:
            sync.return_value = []
            await self.bot.setup_hook()
        self.assertEqual([call.kwargs['guild'].id for call in sync.await_args_list], [123, 456])
        for gid in (123, 456):
            self.assertEqual([c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=gid))], ['ping'])
        self.fetch_guilds.assert_not_called()

    async def test_global_mode_clears_remote_guilds_before_sync(self):
        async def guilds(**kwargs):
            self.assertIsNone(kwargs['limit'])
            for gid in (123, 456, 789):
                yield discord.Object(id=gid)
        self.fetch_guilds.side_effect = guilds
        async def old_commands(*, guild):
            return [] if guild.id == 456 else [SimpleNamespace(name='old')]
        order = []
        async def sync(*, guild):
            order.append(guild.id if guild else None)
            if guild:
                self.assertEqual(self.bot.tree.get_commands(guild=guild), [])
                return []
            self.assertEqual([c.name for c in self.bot.tree.get_commands()], ['ping'])
            return [SimpleNamespace(name='ping', id=1)]
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command), \
                patch.object(self.bot.tree, 'fetch_commands', side_effect=old_commands), \
                patch.object(self.bot.tree, 'sync', side_effect=sync):
            await self.bot.setup_hook()
        self.assertEqual(order, [123, 789, None])

    async def test_cleanup_failure_stops_before_global_sync(self):
        async def guilds(**kwargs):
            yield discord.Object(id=123)
        self.fetch_guilds.side_effect = guilds
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command), \
                patch.object(self.bot.tree, 'fetch_commands', return_value=[SimpleNamespace(name='old')]), \
                patch.object(self.bot.tree, 'sync', side_effect=RuntimeError('cleanup failed')) as sync, \
                self.assertLogs('ourbot', level='ERROR'):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                await self.bot.setup_hook()
        self.assertEqual(sync.await_count, 1)
        self.assertEqual(sync.await_args.kwargs['guild'].id, 123)
        self.assertEqual([c.name for c in self.bot.tree.get_commands()], ['ping'])

    async def test_guild_list_failure_prevents_all_sync(self):
        self.fetch_guilds.side_effect = RuntimeError('listing failed')
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command), \
                patch.object(self.bot.tree, 'sync', new_callable=AsyncMock) as sync, \
                self.assertLogs('ourbot', level='ERROR'):
            with self.assertRaisesRegex(RuntimeError, 'listing failed'):
                await self.bot.setup_hook()
        sync.assert_not_awaited()

    async def test_extension_failure_never_syncs_partial_tree(self):
        with patch.object(self.bot, 'load_extension', side_effect=RuntimeError('broken extension')), \
                patch.object(self.bot.tree, 'sync', new_callable=AsyncMock) as sync, \
                self.assertLogs('ourbot', level='ERROR'):
            with self.assertRaisesRegex(RuntimeError, 'broken extension'):
                await self.bot.setup_hook()
        sync.assert_not_awaited()

    async def test_empty_tree_does_not_clear_remote_commands(self):
        with patch.object(self.bot, 'load_extension', new_callable=AsyncMock), \
                patch.object(self.bot.tree, 'sync', new_callable=AsyncMock) as sync:
            with self.assertRaisesRegex(RuntimeError, '沒有載入'):
                await self.bot.setup_hook()
        sync.assert_not_awaited()

    async def test_sync_failure_aborts_startup_with_diagnostic(self):
        error = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                  {'code': 50001, 'message': 'Missing Access'})
        with patch.object(self.bot, 'load_extension', side_effect=self.install_command), \
                patch.object(self.bot.tree, 'sync', side_effect=error), \
                self.assertLogs('ourbot', level='ERROR') as logs:
            with self.assertRaises(discord.Forbidden):
                await self.bot.setup_hook()
        self.assertIn('applications.commands', '\n'.join(logs.output))

    def test_sync_target_validation(self):
        self.assertEqual(sync_guild_ids(''), [])
        self.assertEqual(sync_guild_ids('123, 456,123,'), [123, 456])
        for value in ('abc', '-1', '0', str(2**64), '１２３'):
            with self.assertRaises(ValueError):
                sync_guild_ids(value)

    async def test_payload_explicitly_supports_server_install(self):
        await self.install_command('test')
        command = self.bot.tree.get_commands()[0]
        self.assertEqual(command.to_dict(self.bot.tree)['integration_types'], [0])
        discord.app_commands.guild_only()(command)
        self.assertEqual(command.to_dict(self.bot.tree)['contexts'], [0])
        self.assertEqual(command.to_dict(self.bot.tree)['integration_types'], [0])

    async def test_remote_verification_reports_wrong_scope_and_overwrite(self):
        remote = {'id': '2', 'name': 'test',
                  'integration_types': [1], 'contexts': [1], 'default_member_permissions': None}
        with patch.object(self.bot.http, 'get_global_commands', new_callable=AsyncMock, return_value=[remote]), \
                self.assertLogs('ourbot', level='INFO') as logs:
            await OurBot.verify_global_commands(self.bot, [SimpleNamespace(id=1)])
        output = '\n'.join(logs.output)
        self.assertIn('不一致', output)
        self.assertIn('不允許伺服器', output)

    async def test_raw_verification_preserves_zero_context_and_permissions(self):
        remote = {'id': '1', 'name': 'test', 'integration_types': [0],
                  'contexts': [0], 'default_member_permissions': '8'}
        with patch.object(self.bot.http, 'get_global_commands', new_callable=AsyncMock, return_value=[remote]), \
                self.assertLogs('ourbot', level='INFO') as logs:
            await OurBot.verify_global_commands(self.bot, [SimpleNamespace(id=1)])
        self.assertFalse(any(record.levelname == 'WARNING' for record in logs.records))
        self.assertIn('integration_types=[0]', '\n'.join(logs.output))
        self.assertIn('contexts=[0]', '\n'.join(logs.output))
        self.assertIn('default_member_permissions=8', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
