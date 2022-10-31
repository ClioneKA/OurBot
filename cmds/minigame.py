import asyncio
import discord
from discord.ext import commands
from core.classes import Cog_Extension

from collections.abc import Sequence


def make_sequence(seq):
    if seq is None:
        return ()
    if isinstance(seq, Sequence) and not isinstance(seq, str):
        return seq
    else:
        return (seq, )


def message_check(channel=None,
                  author=None,
                  content=None,
                  ignore_bot=True,
                  lower=True):
    channel = make_sequence(channel)
    author = make_sequence(author)
    content = make_sequence(content)
    if lower:
        content = tuple(c.lower() for c in content)

    def check(message):
        if ignore_bot and message.author.bot:
            return False
        if channel and message.channel not in channel:
            return False
        if author and message.author not in author:
            return False
        actual_content = message.content.lower() if lower else message.content
        if content and actual_content not in content:
            return False
        return True

    return check


srp = {'1': '剪刀', '2': '石頭', '3': '布'}
challengerList = []


class Minigame(Cog_Extension):
    @commands.command(name='單挑', help='單挑 [@Mention]')
    async def challenge(self, ctx, target: discord.User):
        print(ctx.message.author)
        print(target.name)
        if (ctx.message.author == target):
            await ctx.send('請不要跟自己決鬥，你個智障。')
            return
        if (target.bot == True):
            await ctx.send('請不要跟機器人決鬥，你個智障。')
            return
        if (ctx.message.author in challengerList):
            await ctx.send('你正在決鬥中，專心好嗎。')
            return
        if (target in challengerList):
            await ctx.send('對方正在決鬥中，請稍候。')
            return
        challengerList.append(ctx.message.author)
        challengerList.append(target)
        await ctx.send(f'{ctx.message.author.mention}已發起對{target.mention}的決鬥！！'
                       )
        fightDone = False
        while not fightDone:
            hostDone = False
            while not hostDone:
                try:
                    await ctx.author.send("請選擇你要出的拳 1-剪刀 2-石頭 3-布")
                except discord.errors.Forbidden:
                    await ctx.send(f'{ctx.message.author.mention}不接受私人訊息，決鬥取消！'
                                   )
                    fightDone = True
                    break
                try:
                    responseHost = await self.bot.wait_for(
                        'message',
                        check=message_check(channel=ctx.author.dm_channel),
                        timeout=20.0)
                except asyncio.TimeoutError:
                    await ctx.send(f'{ctx.message.author.mention}毫無反應，決鬥取消！')
                    fightDone = True
                    break
                if responseHost.content not in ['1', '2', '3']:
                    await ctx.author.send("請不要輸入1, 2, 3以外的選項！87！")
                else:
                    hostDone = True
            if fightDone:
                break
            targetDone = False
            while not targetDone:
                try:
                    await target.send('請選擇你要出的拳 1-剪刀 2-石頭 3-布')
                except discord.errors.Forbidden:
                    await ctx.send(f'{target.mention}不接受私人訊息，決鬥取消！')
                    fightDone = True
                    break
                try:
                    responseTarget = await self.bot.wait_for(
                        'message',
                        check=message_check(channel=target.dm_channel),
                        timeout=20.0)
                except asyncio.TimeoutError:
                    await ctx.send(f'{target.mention}毫無反應，決鬥取消！')
                    fightDone = True
                    break
                if responseTarget.content not in ['1', '2', '3']:
                    await target.send("請不要輸入1, 2, 3以外的選項！87！")
                else:
                    targetDone = True
            if fightDone:
                break
            if responseHost.content == responseTarget.content:
                result = '平手！決鬥繼續！'
            else:
                fightDone = True
                if responseHost.content > responseTarget.content:
                    if responseHost.content == '3' and responseTarget.content == '1':
                        result = f'{target.mention}勝！'
                    else:
                        result = f'{ctx.message.author.mention}勝！'
                else:
                    if responseHost.content == '1' and responseTarget.content == '3':
                        result = f'{ctx.message.author.mention}勝！'
                    else:
                        result = f'{target.mention}勝！'
            await ctx.send(
                f'{ctx.message.author.mention}出{srp[responseHost.content]}！ {target.mention}出{srp[responseTarget.content]}！ {result}'
            )
        challengerList.remove(ctx.message.author)
        challengerList.remove(target)
        print(responseHost.content + responseTarget.content)


async def setup(bot):
    await bot.add_cog(Minigame(bot))
