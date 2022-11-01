import discord
from discord.ext import commands
import random
from core.classes import Cog_Extension


class Main(Cog_Extension):

    goose_emoji = ['<:goose_suck:1021737115412860988>', '<:goose_suck2:1021737380312535130>', '<a:goosepat:847511782121799730>', '<:fkgoose:909346840351227904>', '<:Goose:714794396096135248>']
    #respond
    @commands.command(name='wake', help='wake [@Mention]')
    @commands.is_owner()
    async def wake(self, ctx, target: discord.User):
        print(target)
        await ctx.send(target.name + '起床!', tts=True)
        for i in range(5):
            await target.send('起床!!!')
        await ctx.message.delete()

    @commands.command(name='阿鵝', help='赤色飛燕')
    async def goose(self, ctx):
        await ctx.channel.send(random.choice(goose_emoji))


async def setup(bot):
    await bot.add_cog(Main(bot))
