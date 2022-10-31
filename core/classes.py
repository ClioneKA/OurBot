import discord
from discord.ext import commands
from dotenv import load_dotenv

class Cog_Extension(commands.Cog):
    def __init__(self, bot):
        self.bot = bot