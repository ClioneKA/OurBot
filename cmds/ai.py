import discord
from discord.ext import commands
from core.classes import Cog_Extension
import os
import openai
import io
import aiohttp
import queue

openai.api_key = os.getenv("OPENAI_API_KEY")

bot_name = "Clione"
chat_q = queue.Queue(maxsize=20)


class AI(Cog_Extension):

    @commands.command(name='draw', help='draw a picture.')
    @commands.has_role("VIP")
    async def draw(self, ctx, *, pr):
        # print(pr)
        response = openai.Image.create(
            prompt=pr,
            n=1,
            size="512x512"
        )
        image_url = response['data'][0]['url']
        # print(image_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return await ctx.reply('Could not download file...')
                data = io.BytesIO(await resp.read())
                await ctx.reply(file=discord.File(data, 'cool_image.png'))

    @commands.command(name='chat', help='chat with bot.')
    @commands.has_role("VIP")
    async def chat(self, ctx, *, message):

        if chat_q.full():
            chat_q.get()
        chat_q.put([ctx.message.author.name, message])

        parsed_chat = ""
        for chat in chat_q.queue:
            parsed_chat += chat[0] + ": " + chat[1] + "\n"
        parsed_chat += bot_name + ": "
        print(parsed_chat)
        response = openai.Completion.create(
            model="text-davinci-003",
            prompt=parsed_chat,
            temperature=0.7,
            max_tokens=2048,
            top_p=1,
            frequency_penalty=0.0,
            presence_penalty=0.0,
        )
        responseMessage = response.choices[0].text

        if chat_q.full():
            chat_q.get()
        chat_q.put([bot_name, responseMessage])

        await ctx.reply(responseMessage)





async def setup(bot):
    await bot.add_cog(AI(bot))
