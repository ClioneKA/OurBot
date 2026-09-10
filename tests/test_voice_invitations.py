import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cmds.anan import Anan


class VoiceInvitationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.anan = Anan.__new__(Anan)
        self.anan.connection_locks = defaultdict(asyncio.Lock)
        self.anan.invitation_attempts = {}
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 50
        self.channel.user_limit = 0
        self.channel.members = []
        self.channel.connect = AsyncMock()
        self.permissions = SimpleNamespace(view_channel=True, connect=True)
        self.channel.permissions_for.return_value = self.permissions
        self.author = Mock(spec=discord.Member)
        self.author.voice = SimpleNamespace(channel=self.channel)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None, afk_channel=None)
        self.message = SimpleNamespace(guild=self.guild, author=self.author)

    async def test_accept_connects_once_and_cools_down(self):
        results = await asyncio.gather(*[
            self.anan.accept_voice_invitation(self.message, 50) for _ in range(2)
        ])
        self.assertIsNone(results[0])
        self.assertIsNotNone(results[1])
        self.channel.connect.assert_awaited_once_with(timeout=20, reconnect=False, self_deaf=True)

    async def test_changed_channel_or_missing_permissions_cannot_connect(self):
        self.assertIsNotNone(await self.anan.accept_voice_invitation(self.message, 51))
        self.permissions.connect = False
        self.assertIsNotNone(await self.anan.accept_voice_invitation(self.message, 50))
        self.channel.connect.assert_not_awaited()

    async def test_connection_failure_is_reported_and_cools_down(self):
        self.channel.connect.side_effect = asyncio.TimeoutError()
        with self.assertLogs('cmds.anan', level='ERROR'):
            result = await self.anan.accept_voice_invitation(self.message, 50)
        self.assertIn('連線失敗', result)
        self.assertIsNone(self.anan.invitation_channel(self.message))

    def test_existing_connection_missing_voice_and_full_channel_are_unavailable(self):
        self.guild.voice_client = object()
        self.assertIsNone(self.anan.invitation_channel(self.message))
        self.guild.voice_client = None
        self.author.voice = None
        self.assertIsNone(self.anan.invitation_channel(self.message))
        self.author.voice = SimpleNamespace(channel=self.channel)
        self.channel.user_limit = 1
        self.channel.members = [self.author]
        self.assertIsNone(self.anan.invitation_channel(self.message))


if __name__ == '__main__':
    unittest.main()
