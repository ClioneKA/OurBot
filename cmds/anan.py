import io
import os
import hashlib
import time
import discord
from zoneinfo import ZoneInfo
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
from core.classes import Cog_Extension
from core.gen_image import generate_image
from core.tts import generate_sound
from datetime import datetime

FFMPEG_PATH = os.getenv("FFMPEG_PATH")


class Anan(Cog_Extension):

    @app_commands.command(name="上線", description="叫安安上線")
    @app_commands.default_permissions(administrator=True)
    async def connect(self, interaction: discord.Interaction):
        """connect bot to vc"""
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "你沒有在任何語音頻道內", delete_after=5
            )
            return
        elif voice is None:
            vc = interaction.user.voice.channel
            await vc.connect()
            await interaction.response.send_message("來了", delete_after=5)

    @app_commands.command(name="滾", description="送安安下去")
    @app_commands.default_permissions(administrator=True)
    async def leave(self, interaction: discord.Interaction):
        """disconnect bot from vc"""
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if voice is None:
            await interaction.response.send_message(
                "吾輩沒有在任何語音頻道內", delete_after=5
            )
        else:
            picture = discord.File(
                "images/ananout.png",
                filename="安安出去.jpg",
            )
            await interaction.response.send_message(file=picture, delete_after=5)
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
    async def send_sound(self, interaction: discord.Interaction, text: str):
        """tts by command"""
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
        await self.speak(voice, text)

    async def speak(self, voice, text, emotion=None):
        """tts"""
        if not os.path.exists("./gen_sounds"):
            os.mkdir("./gen_sounds")

        hash_func = hashlib.md5()
        hash_func.update(text.encode("utf-8"))

        fp = f"./gen_sounds/{hash_func.hexdigest()}.mp3"

        if os.path.exists(fp):
            while voice.is_playing():
                time.sleep(1)
            voice.play(discord.FFmpegPCMAudio(executable=FFMPEG_PATH, source=fp))
        else:
            hex_data = generate_sound(text, emotion)
            if hex_data is None:
                return False
            with open(fp, "wb") as f:
                f.write(hex_data)
            while voice.is_playing():
                time.sleep(1)
            voice.play(discord.FFmpegPCMAudio(executable=FFMPEG_PATH, source=fp))
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
            time.sleep(2.5)
            hr = datetime.now().hour
            if hr < 3:
                await self.speak(voice, "おはようございます", "happy")
            elif hr < 10:
                await self.speak(voice, "こんにちは", "happy")
            elif hr < 21:
                await self.speak(voice, "こんばんは", "happy")
            else:
                await self.speak(voice, "おはようございます", "happy")


async def setup(bot):
    await bot.add_cog(Anan(bot))
