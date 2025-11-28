import discord
from discord import app_commands
import random
from core.classes import Cog_Extension

goose_emoji = [
    "<:goosesuck:1021737115412860988>",
    "<:goosesuck2:1021737380312535130>",
    "<:goosepat:847511782121799730>",
    "<:fkgoose:909346840351227904>",
    "<:Goose:714794396096135248>",
]


class Main(Cog_Extension):

    @app_commands.command(name="阿鵝", description="赤色飛燕")
    async def goose(self, interaction: discord.Interaction):
        await interaction.response.send_message(random.choice(goose_emoji))


async def setup(bot):
    await bot.add_cog(Main(bot))
