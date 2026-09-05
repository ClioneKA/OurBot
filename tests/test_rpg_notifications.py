import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
import discord

from core.rpg import RPGStore
from core.rpg_character import CharacterError
from core.rpg_notifications import RaidNotifications


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.service = RaidNotifications(self.store)
        self.role = SimpleNamespace(id=42, mention='<@&42>', managed=False, mentionable=True,
                                   permissions=discord.Permissions.none(), is_default=lambda: False, is_assignable=lambda: True)
        self.roles = {}
        async def create(**kwargs):
            self.assertEqual(kwargs['permissions'].value, 0)
            self.assertTrue(kwargs['mentionable'])
            self.roles[42] = self.role
            return self.role
        self.guild = SimpleNamespace(id=1, get_role=lambda key: self.roles.get(key),
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)),
            create_role=AsyncMock(side_effect=create), fetch_roles=AsyncMock(return_value=[]))
        self.member = SimpleNamespace(add_roles=AsyncMock(), remove_roles=AsyncMock())

    async def test_concurrent_creation_restart_and_opt_out(self):
        await asyncio.gather(self.service.ensure(self.guild), self.service.ensure(self.guild))
        self.guild.create_role.assert_awaited_once()
        restarted = RaidNotifications(self.store)
        await restarted.subscribe(self.guild, self.member)
        self.member.add_roles.assert_awaited_once_with(self.role, reason='玩家領取討伐通知', atomic=True)
        await restarted.subscribe(self.guild, self.member, False)
        self.member.remove_roles.assert_awaited_once()
        self.guild.create_role.assert_awaited_once()

    async def test_permissions_and_missing_subscription(self):
        await self.service.subscribe(self.guild, self.member, False)
        self.guild.create_role.assert_not_awaited()
        self.guild.me.guild_permissions.manage_roles = False
        with self.assertRaises(CharacterError):
            await self.service.subscribe(self.guild, self.member)
        self.guild.me.guild_permissions.manage_roles = True
        await self.service.ensure(self.guild)
        self.role.permissions = discord.Permissions(administrator=True)
        with self.assertRaises(CharacterError):
            await self.service.subscribe(self.guild, self.member)
        self.member.add_roles.assert_not_awaited()

    async def test_mid_tier_role_is_independent(self):
        await self.service.ensure(self.guild)
        second = SimpleNamespace(id=43, mention='<@&43>', managed=False, mentionable=True,
                                 permissions=discord.Permissions.none(), is_default=lambda: False,
                                 is_assignable=lambda: True)
        async def create(**kwargs):
            self.assertEqual(kwargs['name'], '安安大冒險・中階討伐通知')
            self.roles[43] = second
            return second
        self.guild.create_role = AsyncMock(side_effect=create)
        await self.service.subscribe(self.guild, self.member, kind='mid')
        self.member.add_roles.assert_awaited_with(second, reason='玩家領取中階討伐通知', atomic=True)
