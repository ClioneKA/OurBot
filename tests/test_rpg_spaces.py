from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from core.rpg import RPGStore
from core.rpg_spaces import AdventureSpace, AdventureSpaceService, AdventureSpaceStore


class FakeRole:
    def __init__(self, role_id, name='role', *, managed=False, default=False):
        self.id, self.name, self.managed = role_id, name, managed
        self.mention = f'<@&{role_id}>'
        self._default = default

    def is_default(self):
        return self._default

    def is_assignable(self):
        return True


class FakeCategory:
    def __init__(self, channel_id, guild, name='category'):
        self.id, self.guild, self.name = channel_id, guild, name
        self.mention = f'<#{channel_id}>'
        self.overwrites = {}
        self.set_permissions = AsyncMock(side_effect=self._set_permissions)

    def _set_permissions(self, target, overwrite=None, **permissions):
        self.overwrites[target] = overwrite or discord.PermissionOverwrite(**{
            key: value for key, value in permissions.items() if key != 'reason'})

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())


class FakeTextChannel(FakeCategory):
    def __init__(self, channel_id, guild, name, category=None):
        super().__init__(channel_id, guild, name)
        self.category_id = category.id if category else None
        self.edit = AsyncMock(side_effect=self._edit)

    def _edit(self, *, category, **_kwargs):
        self.category_id = category.id
        return self


class AdventureSpaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.rpg = RPGStore(Path(self.directory.name) / 'rpg.db')
        self.addCleanup(self.rpg.close)
        self.default_role = FakeRole(1, '@everyone', default=True)
        self.bot_role = FakeRole(2, '安安')
        self.adventurer_role = FakeRole(3, '冒險者')
        permissions = SimpleNamespace(manage_channels=True, manage_roles=True)
        self.bot_member = FakeRole(4, '安安成員')
        self.bot_member.guild_permissions = permissions
        self.channels = {}
        self.roles = {1: self.default_role, 3: self.adventurer_role}
        self.members = {}
        self.next_id = 100

        async def create_role(**_kwargs):
            role = FakeRole(self._id(), '安安大冒險・冒險者')
            self.roles[role.id] = role
            return role

        async def create_category(name, overwrites=None, **_kwargs):
            category = FakeCategory(self._id(), self.guild, name)
            category.overwrites.update(overwrites or {})
            self.channels[category.id] = category
            return category

        async def create_text_channel(name, *, category, overwrites, **_kwargs):
            channel = FakeTextChannel(self._id(), self.guild, name, category)
            channel.overwrites.update(overwrites)
            self.channels[channel.id] = channel
            return channel

        self.guild = SimpleNamespace(
            id=9, default_role=self.default_role, me=self.bot_member,
            get_role=lambda role_id: self.roles.get(role_id),
            get_channel=lambda channel_id: self.channels.get(channel_id),
            get_member=lambda user_id: self.members.get(user_id),
            create_role=AsyncMock(side_effect=create_role),
            create_category=AsyncMock(side_effect=create_category),
            create_text_channel=AsyncMock(side_effect=create_text_channel))
        notifications = SimpleNamespace(ensure=AsyncMock(return_value=None))
        raids = SimpleNamespace(environment_channels=set(), environment_mid_channels=set(),
                                environment_high_channels=set(), notifications=notifications,
                                refresh_channels=Mock())
        self.cog = SimpleNamespace(store=self.rpg, raids=raids,
                                   tavern=SimpleNamespace(environment_channel_ids=()),
                                   invitations=SimpleNamespace(configured_role_ids=()))
        self.service = AdventureSpaceService(self.cog)
        self.cog.spaces = self.service

    def _id(self):
        self.next_id += 1
        return self.next_id

    async def test_store_round_trip(self):
        store = AdventureSpaceStore(self.rpg)
        expected = AdventureSpace(9, 10, 11, 12, 13, 14, 15)
        store.save(expected)
        self.assertEqual(store.get(9), expected)
        self.assertIn(expected, store.all())

    async def test_setup_adopts_environment_and_repair_applies_permissions(self):
        category = FakeCategory(10, self.guild, '既有分類')
        self.channels[10] = category
        values = []
        for channel_id, name in enumerate(('一般', '中階', '高階', '酒館'), 11):
            self.channels[channel_id] = FakeTextChannel(channel_id, self.guild, name, category)
            values.append(channel_id)
        self.cog.raids.environment_channels = {11}
        self.cog.raids.environment_mid_channels = {12}
        self.cog.raids.environment_high_channels = {13}
        self.cog.tavern.environment_channel_ids = (14,)
        self.cog.invitations.configured_role_ids = (3,)

        with patch('core.rpg_spaces.discord.TextChannel', FakeTextChannel), \
                patch('core.rpg_spaces.discord.CategoryChannel', FakeCategory):
            message = await self.service.setup(self.guild)
            self.assertIn('沒有建立新頻道', message)
            self.guild.create_category.assert_not_awaited()
            self.guild.create_text_channel.assert_not_awaited()
            self.assertEqual(self.service.store.get(9), AdventureSpace(9, 10, 11, 12, 13, 14, 3))
            self.assertIn('必要權限尚未一致', self.service.status_text(self.guild))

            await self.service.repair(self.guild)
            self.assertIs(self.channels[11].overwrites_for(self.adventurer_role).send_messages, False)
            self.assertIs(self.channels[14].overwrites_for(self.adventurer_role).send_messages, True)
            self.assertIs(self.channels[11].overwrites_for(self.default_role).view_channel, False)
            self.assertIn('皆正常', self.service.status_text(self.guild))

        self.cog.raids.refresh_channels.assert_called()
        self.assertGreaterEqual(self.cog.raids.notifications.ensure.await_count, 3)

    async def test_setup_creates_only_missing_objects(self):
        self.rpg.create_player(9, 55)
        member = SimpleNamespace(id=55, bot=False, roles=[], add_roles=AsyncMock())
        self.members[55] = member
        with patch('core.rpg_spaces.discord.TextChannel', FakeTextChannel), \
                patch('core.rpg_spaces.discord.CategoryChannel', FakeCategory):
            await self.service.setup(self.guild)
            space = self.service.store.get(9)
            self.assertIsNotNone(space.category_id)
            self.assertIsNotNone(space.adventurer_role_id)
            self.assertEqual(self.guild.create_text_channel.await_count, 4)
            self.assertEqual(self.channels[space.tavern_channel_id].name, '冒險者酒館')
            self.assertIn('皆正常', self.service.status_text(self.guild))
            member.add_roles.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
