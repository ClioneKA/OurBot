import os
import random
import asyncio
import keep_alive
import time
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
##GUILD = os.getenv('DISCORD_GUILD')

bot = discord.Client(intents=discord.Intents.default())


wilderness_event = [
    'Spider Swarm', 'Unnatural Outcrop', 'Demon Stragglers', 'Butterfly Swarm',
    'King Black Dragon Rampage', 'Forgotten Soldiers', 'Surprising Seedlings',
    'Hellhound Pack', 'Infernal Star', 'Lost Souls', 'Ramokee Incursion',
    'Displaced Energy', 'Evil Bloodwood Tree'
]


@bot.command()
@commands.is_owner()
async def load(ctx, extension):
    bot.load_extension(f'cmds.{extension}')
    await ctx.send(f'Loaded {extension}.')
    await ctx.message.delete()


@bot.command()
@commands.is_owner()
async def unload(ctx, extension):
    bot.unload_extension(f'cmds.{extension}')
    await ctx.send(f'Unloaded {extension}.')
    await ctx.message.delete()


@bot.command()
@commands.is_owner()
async def reload(ctx, extension):
    bot.reload_extension(f'cmds.{extension}')
    await ctx.send(f'Reloaded {extension}.')
    await ctx.message.delete()


for filename in os.listdir('./cmds'):
    if filename.endswith('.py'):
        bot.load_extension(f'cmds.{filename[:-3]}')


def seconds_until_event():
    now = datetime.now()
    target = now.replace(minute=55, second=0, microsecond=0)
    diff = (target - now).total_seconds()
    print(f"{target} - {now} = {diff}")

    if diff < 0:
        diff += 3600

    return diff


@tasks.loop(seconds=1)
async def called_once_an_hour_at_55():
    await asyncio.sleep(seconds_until_event())
    message_channel = bot.get_channel(1036650756046069931)
    print(f"Got channel {message_channel}")
    ts = time.time()
    hours = int(ts / 3600) % 13
    rotation = hours - 4

    if (wilderness_event[rotation] == 'King Black Dragon Rampage'
            or wilderness_event[rotation] == 'Infernal Star'
            or wilderness_event[rotation] == 'Evil Bloodwood Tree'):
        await message_channel.send(
            "<@&1036645844964888668> Event Coming!! Next event: " +
            wilderness_event[rotation])
    else:
        await message_channel.send("Event Coming!! Next event: " +
                                   wilderness_event[rotation])


@called_once_an_hour_at_55.before_loop
async def before():
    await bot.wait_until_ready()
    print("Finished waiting")


called_once_an_hour_at_55.start()
bot.run(TOKEN)
