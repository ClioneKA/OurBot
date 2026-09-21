"""Publish Anan Adventure release notes once per server after deployment."""

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from core.update_announcements import Release, UpdateAnnouncementStore


ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class UpdateAnnouncements(commands.Cog):
    def __init__(self, bot, *, release_path=None, database_path=None):
        self.bot = bot
        self.release = Release.load(release_path or ROOT / "config/update.toml")
        self.store = UpdateAnnouncementStore(database_path or ROOT / "data/updates.db")
        self.publish_lock = asyncio.Lock()
        self.startup_task = None

    async def cog_load(self):
        self.startup_task = asyncio.create_task(self.publish_after_ready())

    async def cog_unload(self):
        if self.startup_task:
            self.startup_task.cancel()
        self.store.close()

    async def publish_after_ready(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self.publish_to(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self.publish_to(guild)

    async def publish_to(self, guild):
        if guild.unavailable or self.store.was_published(guild.id, self.release.version):
            return False
        async with self.publish_lock:
            if self.store.was_published(guild.id, self.release.version):
                return False
            try:
                channel = await self.find_or_create_channel(guild)
                message = await channel.send(
                    embed=self.release_embed(), allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, discord.Forbidden, AttributeError):
                logger.exception("無法在伺服器 %s 發布更新 %s", guild.id, self.release.version)
                return False
            self.store.mark_published(
                guild.id, self.release.version, channel.id, message.id)
            logger.info("已在伺服器 %s 的頻道 %s 發布更新 %s",
                        guild.id, channel.id, self.release.version)
            return True

    async def find_or_create_channel(self, guild):
        saved_id = self.store.channel_id(guild.id)
        channel = guild.get_channel(saved_id) if saved_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name=self.release.channel_name)
        if channel is None:
            if guild.me is None:
                raise AttributeError("找不到 Bot 的伺服器成員資料")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=False,
                    create_public_threads=False, create_private_threads=False,
                    send_messages_in_threads=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=True,
                    embed_links=True, manage_messages=True),
            }
            channel = await guild.create_text_channel(
                self.release.channel_name, topic="安安大冒險版本更新與玩法公告",
                overwrites=overwrites, reason="建立 Bot 更新資訊頻道")
        self.store.save_channel(guild.id, channel.id)
        return channel

    def release_embed(self):
        embed = discord.Embed(
            title=f"📢 {self.release.title}", description=self.release.description,
            color=0xF2B84B)
        embed.set_footer(text=f"版本 {self.release.version}")
        return embed


async def setup(bot):
    await bot.add_cog(UpdateAnnouncements(bot))
