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
        if guild.unavailable:
            return False
        async with self.publish_lock:
            try:
                channel = await self.find_or_create_channel(guild)
                if self.store.was_published(guild.id, self.release.version):
                    return False
                message = await channel.send(
                    embed=self.release_embed(), allowed_mentions=discord.AllowedMentions.none())
            except RuntimeError as exc:
                logger.warning("無法在伺服器 %s 發布更新 %s：%s",
                               guild.id, self.release.version, exc)
                return False
            except (discord.HTTPException, AttributeError):
                logger.exception("無法在伺服器 %s 發布更新 %s", guild.id, self.release.version)
                return False
            self.store.mark_published(
                guild.id, self.release.version, channel.id, message.id)
            logger.info("已在伺服器 %s 的頻道 %s 發布更新 %s",
                        guild.id, channel.id, self.release.version)
            return True

    async def find_or_create_channel(self, guild):
        category, adventurer_role = self.adventure_context(guild)
        if category is None:
            raise RuntimeError("尚未建立或登記安安大冒險分類")
        saved_id = self.store.channel_id(guild.id)
        channel = guild.get_channel(saved_id) if saved_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name=self.release.channel_name)
        if channel is None:
            # Adopt the original channel name so this rename does not create a duplicate.
            channel = discord.utils.get(guild.text_channels, name="安安大冒險更新")
        if channel is None:
            if guild.me is None:
                raise AttributeError("找不到 Bot 的伺服器成員資料")
            channel = await guild.create_text_channel(
                self.release.channel_name, topic="安安大冒險版本更新與玩法公告",
                category=category, reason="建立安安大冒險更新資訊頻道")
        elif channel.name != self.release.channel_name or channel.category_id != category.id:
            await channel.edit(name=self.release.channel_name, category=category,
                               sync_permissions=True, reason="統一更新資訊頻道名稱與分類")
        await self.make_read_only(guild, channel, adventurer_role)
        self.store.save_channel(guild.id, channel.id)
        return channel

    def adventure_context(self, guild):
        rpg = self.bot.get_cog("RPG")
        if rpg is not None:
            space = rpg.spaces.store.get(guild.id)
            category = rpg.spaces._valid_category(
                guild, space.category_id if space else None)
            if category is not None:
                role = rpg.spaces._valid_role(
                    guild, space.adventurer_role_id if space else None)
                return category, role
        return discord.utils.get(guild.categories, name="安安大冒險"), None

    @staticmethod
    async def make_read_only(guild, channel, adventurer_role=None):
        if guild.me is None:
            raise AttributeError("找不到 Bot 的伺服器成員資料")
        everyone = channel.overwrites_for(guild.default_role)
        everyone.update(send_messages=False, create_public_threads=False,
                        create_private_threads=False, send_messages_in_threads=False)
        await channel.set_permissions(
            guild.default_role, overwrite=everyone, reason="限制更新資訊頻道為唯讀")
        if adventurer_role is not None:
            adventurer = channel.overwrites_for(adventurer_role)
            adventurer.update(view_channel=True, read_message_history=True, send_messages=False,
                              create_public_threads=False, create_private_threads=False,
                              send_messages_in_threads=False)
            await channel.set_permissions(
                adventurer_role, overwrite=adventurer, reason="開放冒險者閱讀更新資訊")
        bot = channel.overwrites_for(guild.me)
        bot.update(view_channel=True, read_message_history=True, send_messages=True,
                   embed_links=True, manage_messages=True)
        await channel.set_permissions(
            guild.me, overwrite=bot, reason="允許安安發布更新資訊")

    def release_embed(self):
        embed = discord.Embed(
            title=f"📢 {self.release.title}", description=self.release.description,
            color=0xF2B84B)
        embed.set_footer(text=f"版本 {self.release.version}")
        return embed


async def setup(bot):
    await bot.add_cog(UpdateAnnouncements(bot))
