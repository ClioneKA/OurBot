import discord
from discord.ext import commands
from core.classes import Cog_Extension

class Main(Cog_Extension):
    
    #respond
    @commands.command(name='wake', help='wake [@mention]')
    @commands.is_owner()
    async def wake(self, ctx, target: discord.User):
        print(target)
        await ctx.send(target.name + '起床!', tts=True)
        for i in range(5):
            await target.send('起床!!!')
        await ctx.message.delete()

def setup(bot):
    bot.add_cog(Main(bot))