import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from ourbot import OurBot, sync_guild_ids


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = OurBot()
        self.addAsyncCleanup(self.bot.close)

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


if __name__ == '__main__':
    unittest.main()
