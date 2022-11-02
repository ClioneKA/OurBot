import discord
from discord.ext import commands
from core.classes import Cog_Extension
import os

hosting = {}

class TVC(Cog_Extension):

    @commands.command(name='開房', help='開房 [房名]')
    @commands.has_role("VIP")
    async def start(self, ctx, cname):
        guildID = ctx.guild.id
        id = ctx.author.id
        category = discord.utils.get(ctx.guild.categories, id=1037480274876964904)
        if id in hosting:
            await ctx.channel.send("你已經有房間了！")
            return
        else:
            try:
                channel = await ctx.guild.create_voice_channel(cname, category=category)
                hosting[id] = channel
            except:
                ctx.channel.send("Something went wrong!")


async def setup(bot):
    await bot.add_cog(TVC(bot))
