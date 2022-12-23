import discord
from discord.ext import commands
from core.classes import Cog_Extension
import os
import openai
import io
import aiohttp

openai.api_key = os.getenv("OPENAI_API_KEY")
class AI(Cog_Extension):

    @commands.command(name='draw', help='draw a picture.')
    @commands.has_role("VIP")
    async def draw(self, ctx, *, pr):

        #print(pr)
        response = openai.Image.create(
            prompt=pr,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        #print(image_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return await ctx.reply('Could not download file...')
                data = io.BytesIO(await resp.read())
                await ctx.reply(file=discord.File(data, 'cool_image.png'))

async def setup(bot):
    await bot.add_cog(AI(bot))
