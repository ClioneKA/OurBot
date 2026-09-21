import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cmds.updates import UpdateAnnouncements
from core.update_announcements import Release, UpdateAnnouncementStore


class ReleaseTests(unittest.TestCase):
    def test_project_release_is_valid(self):
        release = Release.load(Path(__file__).parent.parent / "config/update.toml")
        self.assertEqual(release.channel_name, "安安大冒險更新")
        self.assertIn("更新內容", release.description)

    def test_store_remembers_each_guild_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateAnnouncementStore(Path(directory) / "updates.db")
            try:
                self.assertFalse(store.was_published(1, "v1"))
                store.save_channel(1, 20)
                store.mark_published(1, "v1", 20, 30)
                self.assertEqual(store.channel_id(1), 20)
                self.assertTrue(store.was_published(1, "v1"))
                self.assertFalse(store.was_published(2, "v1"))
                self.assertFalse(store.was_published(1, "v2"))
            finally:
                store.close()


class UpdateAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        release_path = root / "update.toml"
        release_path.write_text(
            'version = "v1"\ntitle = "新功能"\nsummary = "摘要"\n'
            'changes = ["項目一"]\nchannel_name = "安安大冒險更新"\n', encoding="utf-8")
        self.bot = SimpleNamespace(wait_until_ready=AsyncMock(), guilds=[])
        self.cog = UpdateAnnouncements(
            self.bot, release_path=release_path, database_path=root / "updates.db")
        self.addAsyncCleanup(self.cog.cog_unload)

    async def test_creates_read_only_channel_and_publishes_once(self):
        sent = SimpleNamespace(id=30)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 20
        channel.send = AsyncMock(return_value=sent)
        guild = SimpleNamespace(
            id=10, unavailable=False, me=object(), default_role=object(), text_channels=[],
            get_channel=Mock(return_value=None),
            create_text_channel=AsyncMock(return_value=channel))

        self.assertTrue(await self.cog.publish_to(guild))
        self.assertFalse(await self.cog.publish_to(guild))
        guild.create_text_channel.assert_awaited_once()
        overwrite = guild.create_text_channel.await_args.kwargs["overwrites"][guild.default_role]
        self.assertFalse(overwrite.send_messages)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.kwargs["embed"].footer.text, "版本 v1")

    async def test_reuses_saved_channel(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id = 20
        channel.send = AsyncMock(return_value=SimpleNamespace(id=30))
        self.cog.store.save_channel(10, 20)
        guild = SimpleNamespace(
            id=10, unavailable=False, me=object(), default_role=object(), text_channels=[],
            get_channel=Mock(return_value=channel), create_text_channel=AsyncMock())

        self.assertTrue(await self.cog.publish_to(guild))
        guild.create_text_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
