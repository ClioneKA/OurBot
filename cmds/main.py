import discord
from discord.ext import commands
import random
from core.classes import Cog_Extension
import requests
import json

goose_emoji = ['<:goose_suck:1021737115412860988>', '<:goose_suck2:1021737380312535130>',
               '<a:goosepat:847511782121799730>', '<:fkgoose:909346840351227904>', '<:Goose:714794396096135248>']


class Main(Cog_Extension):

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

    @commands.command(name='adol', help='每天關心阿斗的Agility等級')
    async def adol(self, ctx):
        gres = requests.get('https://apps.runescape.com/runemetrics/profile/profile?user=nijiiro_nino&activities=20')
        gdata = gres.json()
        for skill in gdata["skillvalues"]:
            if skill["id"] == 16:
                await ctx.channel.send("阿斗現在"+skill["level"].__str__()+"等，距離敏捷大師還差"+((2000000000-skill['xp'])/10).__str__()+"經驗。")

async def setup(bot):
    await bot.add_cog(Main(bot))
