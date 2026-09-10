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

    async def admin_voice_action(self, interaction, action, text=None, language=None):
        """Run panel voice actions; caller defers its private response first."""
        if (interaction.guild is None or not isinstance(interaction.user, discord.Member)
                or not interaction.user.guild_permissions.administrator):
            return "只有伺服器管理員可以操作語音。"
        if action not in {'join', 'leave', 'speak'}:
            return "未知的語音操作。"
        if action == 'speak' and (not isinstance(text, str) or not text.strip() or len(text) > 50):
            return "朗讀文字必須介於 1 到 50 字。"
        try:
            async with self.connection_locks[interaction.guild_id]:
                voice = interaction.guild.voice_client
                if action == 'leave':
                    self.invitation_attempts[interaction.guild_id] = time.monotonic()
                    if voice is None:
                        return "安安目前沒有在語音頻道裡。"
                    await voice.disconnect()
                    return "安安已離開語音頻道。"
                state = interaction.user.voice
                channel = state.channel if state else None
                if not isinstance(channel, discord.VoiceChannel):
                    return "請先加入一般語音頻道。"
                permissions = channel.permissions_for(interaction.guild.me)
                if not permissions.view_channel or not permissions.connect:
                    return "安安沒有加入這個語音頻道的權限。"
                if action == 'speak' and not permissions.speak:
                    return "安安沒有在這個語音頻道發言的權限。"
                if voice is not None and voice.channel != channel:
                    return "安安已在其他語音頻道，請先讓她離開。"
                if voice is None:
                    if channel.user_limit and len(channel.members) >= channel.user_limit:
                        return "這個語音頻道已滿。"
                    voice = await channel.connect(timeout=20, reconnect=False, self_deaf=True)
                if action == 'join':
                    return f"安安已在 {channel.name} 語音頻道。"
            if await asyncio.wait_for(self.speak(voice, text, language=language), timeout=60):
                return "安安已開始朗讀。"
            return "朗讀失敗，請確認語音服務設定與連線。"
        except (discord.DiscordException, asyncio.TimeoutError, OSError, RuntimeError):
            logging.getLogger(__name__).exception('管理面板語音操作失敗')
            return "語音操作失敗，請確認權限與連線後重試。"


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
