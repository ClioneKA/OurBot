import discord
from discord.ext import commands
from core.classes import Cog_Extension
import os
from dotenv import load_dotenv

load_dotenv()
welcomeChannel = os.getenv('WELCOME_CHANNEL')


class Event(Cog_Extension):
    @commands.Cog.listener()
    async def on_member_join(self, member):
        channel = self.bot.get_channel(int(welcomeChannel))
        await channel.send(f'{member} join!')

    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.content == '阿鵝' and msg.author != self.bot.user:
            await msg.channel.send('<:Goose:714794396096135248>')

    # @commands.Cog.listener()
    # async def on_command_error(self, ctx, error):
    #     if hasattr(ctx.command, 'on_error'):
    #         return
    #     if isinstance(error, commands.errors.MissingRequiredArgument):
    #         await ctx.send('Missing parameters!')


async def setup(bot):
    await bot.add_cog(Event(bot))
