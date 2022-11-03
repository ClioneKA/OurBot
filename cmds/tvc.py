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
        elif ctx.author.voice is None:
            await ctx.channel.send("請先進入任意一語音頻道！")
        else:
            try:
                channel = await ctx.guild.create_voice_channel(cname, category=category)
                hosting[id] = channel
                await ctx.author.move_to(channel)

                def check(member, before, after):
                    return member == ctx.author and before.channel == channel and after.channel is not channel

                await self.bot.wait_for("voice_state_update", check=check)
                del hosting[id]
                await channel.delete()
            except Exception as e:
                print(e);
                await ctx.channel.send("Something went wrong!")



async def setup(bot):
    await bot.add_cog(TVC(bot))
