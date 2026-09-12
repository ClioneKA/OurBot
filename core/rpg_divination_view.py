"""Private movement and tarot-room panels."""
import asyncio

import discord

from core.rpg_character import CharacterError
from core.rpg_divination import CARDS
from core.rpg_menu import add_help, add_back, navigate


class DivinationView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        status = self.cog.divinations.status(self.guild_id, self.owner.id)
        draw = discord.ui.Button(label=f'占卜（{status["next_price"]:,} 金幣）',
                                 style=discord.ButtonStyle.primary, row=0)
        async def draw_callback(interaction):
            await self.handle(interaction, 'draw')
        draw.callback = draw_callback
        self.add_item(draw)
        if (status['card'] == 'high_priestess' and status['summon_raid_id'] is None
                and status['bound_raid_id'] is None):
            summon = discord.ui.Button(label='揭開帷幕，發起討伐', style=discord.ButtonStyle.danger, row=0)
            async def summon_callback(interaction):
                await self.handle(interaction, 'summon')
            summon.callback = summon_callback
            self.add_item(summon)
        add_help(self, 1, 'life', 'divination')
        back = discord.ui.Button(label='返回移動', row=1)
        async def back_callback(interaction):
            await self.handle(interaction, 'travel')
        back.callback = back_callback
        self.add_item(back)
        add_back(self, 1)
        refresh = discord.ui.Button(label='重新整理', row=1)
        close = discord.ui.Button(label='關閉', row=1)
        async def refresh_callback(interaction):
            await self.handle(interaction, 'refresh')
        async def close_callback(interaction):
            await self.handle(interaction, 'close')
        refresh.callback, close.callback = refresh_callback, close_callback
        self.add_item(refresh)
        self.add_item(close)

    def embed(self, notice=None):
        status = self.cog.divinations.status(self.guild_id, self.owner.id)
        card = CARDS.get(status['card'])
        if card:
            active = f'**{card.name}**\n{card.omen}\n{card.effect}\n本場討伐經驗 +10%'
            if status['bound_raid_id']:
                active += '\n\n此效果已綁定正在進行的討伐。'
            elif status['summon_raid_id']:
                active += '\n\n女祭司已經揭開帷幕；你已自動報名該場討伐。'
        else:
            active = '目前沒有占卜效果。'
        embed = discord.Embed(
            title='安安大冒險｜瑪格的占卜室',
            description=(f'{active}\n\n今日已占卜 **{status["draws"]}** 次；'
                         f'下次需要 **{status["next_price"]:,} 金幣**。\n'
                         '每天不限次數，每次價格增加 300 金幣；每日 00:00 重置價格。'
                         '再次占卜會覆蓋尚未使用的牌，參加的討伐結束後效果清空。'),
            color=0x6D3A8D)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='所有牌都會使下一場討伐取得的經驗增加 10%。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請從自己的移動頁進入占卜室。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('占卜室已關閉，請從 /冒險 重新進入。', ephemeral=True)
                return
            if action in ('home', 'travel'):
                await navigate(self, interaction, action)
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='你離開了瑪格的占卜室。', embed=None, view=None)
                return
            if action == 'summon':
                await interaction.response.defer()
                try:
                    channel, raid = await self.cog.raids.summon_divination(interaction.guild, self.owner)
                    notice = f'女祭司揭開了帷幕，已在 {channel.mention} 發起討伐；你已自動報名。'
                except CharacterError as exc:
                    notice = str(exc)
                except discord.HTTPException:
                    notice = '討伐發布失敗，女祭司仍然保留，請稍後再試。'
                self.rebuild()
                await interaction.edit_original_response(embed=self.embed(notice), view=self)
                return
            notice = None
            if action == 'draw':
                try:
                    card_id, cost = self.cog.divinations.draw(self.guild_id, self.owner.id)
                    card = CARDS[card_id]
                    notice = f'瑪格翻開了「{card.name}」。已支付 {cost:,} 金幣。\n{card.omen}'
                except CharacterError as exc:
                    notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='占卜室已逾時，請從 /冒險 重新進入。', view=None)
            except discord.HTTPException:
                pass
