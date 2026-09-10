import asyncio
import io
import os
import time
import logging
from collections import defaultdict
import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
from core.classes import Cog_Extension
from core.gen_image import generate_image
from core.tts import get_cached_sound
from core.settings import get_settings
from datetime import datetime

FFMPEG_PATH = os.getenv("FFMPEG_PATH")


class Anan(Cog_Extension):

    def __init__(self, bot):
        super().__init__(bot)
        self.voice_locks = defaultdict(asyncio.Lock)
        self.connection_locks = defaultdict(asyncio.Lock)
        self.invitation_attempts = {}

    def invitation_channel(self, message):
        if not get_settings().ai.media.voice_invitations_enabled or message.guild is None:
            return None
        if not isinstance(message.author, discord.Member):
            return None
        state = message.author.voice
        channel = state.channel if state else None
        if not isinstance(channel, discord.VoiceChannel) or channel == message.guild.afk_channel:
            return None
        if message.guild.voice_client is not None or message.guild.me is None:
            return None
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.view_channel or not permissions.connect:
            return None
        if channel.user_limit and len(channel.members) >= channel.user_limit:
            return None
        cooldown = get_settings().ai.media.voice_invitation_cooldown_seconds
        if time.monotonic() - self.invitation_attempts.get(message.guild.id, float('-inf')) < cooldown:
            return None
        return channel

    async def accept_voice_invitation(self, message, channel_id):
        async with self.connection_locks[message.guild.id]:
            channel = self.invitation_channel(message)
            if channel is None or channel.id != channel_id:
                return "吾輩本來想過去，但現在沒辦法加入你的語音頻道，待會再邀吾輩吧。"
            self.invitation_attempts[message.guild.id] = time.monotonic()
            try:
                await channel.connect(timeout=20, reconnect=False, self_deaf=True)
            except (discord.DiscordException, asyncio.TimeoutError, OSError):
                logging.getLogger(__name__).exception("受邀加入語音頻道失敗")
                return "吾輩想過去，但語音連線失敗了……待會再試吧。"
        return None

    async def _require_administrator(
        self, interaction: discord.Interaction
    ) -> bool:
        administrator = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )
        if administrator:
            return True
        await interaction.response.send_message(
            "只有伺服器管理員可以使用這個指令。", ephemeral=True
        )
        return False

    @app_commands.command(name="上線", description="叫安安上線")
    @app_commands.default_permissions(administrator=True)
    async def connect(self, interaction: discord.Interaction):
        """connect bot to vc"""
        if not await self._require_administrator(interaction):
            return
        await interaction.response.defer()
        async with self.connection_locks[interaction.guild_id]:
            await self._connect_manually(interaction)

    async def _connect_manually(self, interaction):
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if interaction.user.voice is None:
            await interaction.followup.send(
                "你沒有在任何語音頻道內", delete_after=5
            )
            return
        elif voice is None:
            vc = interaction.user.voice.channel
            await vc.connect()
            await interaction.followup.send("來了", delete_after=5)
        else:
            await interaction.followup.send(
                "吾輩已經在語音頻道裡了", delete_after=5
            )

    @app_commands.command(name="滾", description="送安安下去")
    @app_commands.default_permissions(administrator=True)
    async def leave(self, interaction: discord.Interaction):
        """disconnect bot from vc"""
        if not await self._require_administrator(interaction):
            return
        await interaction.response.defer()
        async with self.connection_locks[interaction.guild_id]:
            await self._leave_manually(interaction)

    async def _leave_manually(self, interaction):
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if voice is None:
            await interaction.followup.send(
                "吾輩沒有在任何語音頻道內", delete_after=5
            )
        else:
            self.invitation_attempts[interaction.guild.id] = time.monotonic()
            picture = discord.File(
                "images/ananout.png",
                filename="安安出去.jpg",
            )
            await interaction.followup.send(file=picture, delete_after=5)
            await voice.disconnect()

    @app_commands.command(name="安安傳話筒", description="請安安幫你說不想直接說的話")
    @app_commands.describe(text="輸入要說的話", emotion="安安的表情")
    @app_commands.choices(
        emotion=[
            Choice(name="普通", value="普通"),
            Choice(name="開心", value="開心"),
            Choice(name="生氣", value="生氣"),
            Choice(name="無語", value="無語"),
            Choice(name="臉紅", value="臉紅"),
            Choice(name="病嬌", value="病嬌"),
            Choice(name="閉眼", value="閉眼"),
            Choice(name="難受", value="難受"),
            Choice(name="害怕", value="害怕"),
            Choice(name="激動", value="激動"),
            Choice(name="驚訝", value="驚訝"),
            Choice(name="哭泣", value="哭泣"),
        ],
    )
    async def send_image(
        self, interaction: discord.Interaction, text: str, emotion: str
    ):
        """send generated image to chat"""
        image_bytes = generate_image(text, emotion)
        if image_bytes == None:
            await interaction.response.send_message("嗚嗚~素描本沒紙了")
        else:
            file = discord.File(fp=io.BytesIO(image_bytes), filename="anan.jpg")
            await interaction.response.send_message(file=file)

    @app_commands.command(name="洗腦", description="安安的固有魔法")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(language="朗讀語言；日文漢字請選日文，避免誤讀成中文")
    @app_commands.rename(language="語言")
    @app_commands.choices(language=[
        Choice(name="自動辨識", value="auto"),
        Choice(name="日文", value="Japanese"),
        Choice(name="中文", value="Chinese"),
        Choice(name="英文", value="English"),
    ])
    async def send_sound(self, interaction: discord.Interaction, text: str, language: str = None):
        """tts by command"""
        if not await self._require_administrator(interaction):
            return
        if len(text) > 50:
            await interaction.response.send_message(
                "嗚~安安不想說那麼多話", delete_after=5
            )
            return

        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)

        if interaction.user.voice is None:
            await interaction.response.send_message(
                "你沒有在任何語音頻道內", delete_after=5
            )
            return
        elif voice is None:
            vc = interaction.user.voice.channel
            await vc.connect()

        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)

        await interaction.response.send_message("魔法，很神奇吧", delete_after=5)
        await self.speak(voice, text, language=language)

    async def speak(self, voice, text, emotion=None, language=None):
        """tts"""
        async with self.voice_locks[voice.guild.id]:
            audio = await get_cached_sound(text, emotion, language=language)
            if audio is None:
                return False

            while voice.is_playing():
                await asyncio.sleep(0.25)
            if not voice.is_connected():
                return False
            voice.play(
                discord.FFmpegPCMAudio(
                    executable=FFMPEG_PATH,
                    source=io.BytesIO(audio),
                    pipe=True,
                )
            )
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """welcome voice when someone join vc"""
        voice = discord.utils.get(self.bot.voice_clients, guild=member.guild)
        if (
            not member.bot
            and voice is not None
            and after.channel == voice.channel
            and before.channel != after.channel
        ):
            await asyncio.sleep(2.5)
            hr = datetime.now().hour
            if hr < 3:
                await self.speak(voice, "おはようございます", "happy", language="Japanese")
            elif hr < 10:
                await self.speak(voice, "こんにちは", "happy", language="Japanese")
            elif hr < 21:
                await self.speak(voice, "こんばんは", "happy", language="Japanese")
            else:
                await self.speak(voice, "おはようございます", "happy", language="Japanese")


async def setup(bot):
    await bot.add_cog(Anan(bot))
