import discord
from discord import app_commands
import os
from core.classes import Cog_Extension
from pytubefix import YouTube

playing_list = []
ffmpeg_path = os.getenv("FFMPEG_PATH")


class Music(Cog_Extension):

    def end_song(self, path):
        # 播放完後的步驟, 進行前一首歌刪除, 抓取一首清單內的歌進行播放
        os.remove(path)
        if len(playing_list) != 0:
            voice = discord.utils.get(self.bot.voice_clients)
            url = playing_list[0]
            del playing_list[0]

            YouTube(url, "WEB").streams.first().download()
            for file in os.listdir("./"):
                if file.endswith(".mp4"):
                    os.rename(file, "song.mp4")

            voice.play(
                discord.FFmpegPCMAudio(executable=ffmpeg_path, source="song.mp4"),
                after=lambda x: self.end_song("song.mp4"),
            )

    @app_commands.command(name="play", description="請安安播放YouTube音樂")
    @app_commands.describe(url="YouTube網址")
    async def play(self, interaction: discord.Interaction, url: str):

        # 取得目前機器人狀態
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)

        if interaction.user.voice == None:
            await interaction.response.send_message(
                "你沒有在任何語音頻道內", delete_after=10
            )
        elif voice == None:
            voiceChannel = interaction.user.voice.channel
            await voiceChannel.connect()

        # 取得目前機器人狀態
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)

        # 如果機器人正在播放音樂, 將音樂放入播放清單
        if voice.is_playing():
            playing_list.append(url)
            await interaction.response.send_message(
                f"目前吾輩的素描本裡還有{len(playing_list)}首歌待播",
                delete_after=10,
            )
        # 如果機器人沒在播放, 開始準備要播放的音樂
        else:
            # 如果還有找到之前已經被播放過的音樂檔, 進行刪除
            song_there = os.path.isfile("song.mp4")

            try:
                if song_there:
                    os.remove("song.mp4")
            except PermissionError:
                await interaction.response.send_message(
                    "Wait for the current playing music to end or use the 'stop' command",
                    delete_after=10,
                )

            # 找尋輸入的Youtube連結, 將目標影片下載下來備用
            YouTube(url, "WEB").streams.first().download()

            # 將目標影片改名, 方便找到它
            for file in os.listdir("./"):
                if file.endswith(".mp4"):
                    os.rename(file, "song.mp4")
            # 找尋要播放的音樂並播放, 結束後依照after部分的程式進行後續步驟
            await interaction.response.send_message("【聽吾輩唱歌】", delete_after=10)
            voice.play(
                discord.FFmpegPCMAudio(executable=ffmpeg_path, source="song.mp4"),
                after=lambda x: self.end_song("song.mp4"),
            )

    @app_commands.command(name="leave", description="送安安下去")
    async def leave(self, interaction: discord.Interaction):
        voice = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if voice == None:
            await interaction.response.send_message(
                "吾輩沒有在任何語音頻道內", delete_after=10
            )
        else:
            picture = discord.File(
                "images/ananout.png",
                filename="安安出去.jpg",
            )
            await interaction.response.send_message(file=picture, delete_after=10)
            await voice.disconnect()


async def setup(bot):
    await bot.add_cog(Music(bot))
