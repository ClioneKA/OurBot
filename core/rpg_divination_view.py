"""Private tarot-room panel."""
import asyncio

import discord

from core.rpg_character import CharacterError
from core.rpg_divination import CARDS, card_rarity
from core.rpg_menu import add_back, add_favorite_toggle, add_help, navigate


CATEGORY_ICON = {'戰鬥': '⚔️', '生活': '🌿', '特殊': '🔮'}


class DivinationView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.confirming_reveal = False
        self.showing_catalog = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        status = self.cog.divinations.status(self.guild_id, self.owner.id)
        if self.showing_catalog:
            back_to_room = discord.ui.Button(label='返回占卜', style=discord.ButtonStyle.primary, row=0)
            back_to_room.callback = lambda interaction: self.handle(interaction, 'room')
            self.add_item(back_to_room)
        elif status['offer']:
            self.confirming_reveal = False
            for card_id in status['offer']:
                card = CARDS[card_id]
                button = discord.ui.Button(
                    label=f'{CATEGORY_ICON[card.category]} {card.name}',
                    style=discord.ButtonStyle.primary, row=0)
                button.callback = lambda interaction, card_id=card_id: self.handle(
                    interaction, 'choose', card_id)
                self.add_item(button)
        elif self.confirming_reveal:
            keep = discord.ui.Button(label='保留目前命運', style=discord.ButtonStyle.secondary, row=0)
            reveal = discord.ui.Button(
                label=f'確認揭牌（{status["next_price"]:,} 金幣）',
                style=discord.ButtonStyle.danger, row=0)
            keep.callback = lambda interaction: self.handle(interaction, 'keep')
            reveal.callback = lambda interaction: self.handle(interaction, 'reveal_confirm')
            self.add_item(keep)
            self.add_item(reveal)
        else:
            price = '免費' if status['next_price'] == 0 else f'{status["next_price"]:,} 金幣'
            reveal = discord.ui.Button(label=f'揭示三張牌（{price}）',
                                       style=discord.ButtonStyle.primary, row=0)
            reveal.callback = lambda interaction: self.handle(interaction, 'reveal')
            self.add_item(reveal)
        if (not self.showing_catalog and not status['offer'] and not self.confirming_reveal
                and status['card'] == 'high_priestess'
                and status['summon_raid_id'] is None):
            summon = discord.ui.Button(label='揭開帷幕，發起討伐',
                                       style=discord.ButtonStyle.danger, row=0)
            summon.callback = lambda interaction: self.handle(interaction, 'summon')
            self.add_item(summon)
        if not self.showing_catalog:
            catalog = discord.ui.Button(label='命運圖鑑', row=0)
            catalog.callback = lambda interaction: self.handle(interaction, 'catalog')
            self.add_item(catalog)
        add_help(self, 1, 'town', 'divination')
        add_back(self, 1, 'travel', '返回冒險')
        refresh = discord.ui.Button(label='重新整理', row=1)
        close = discord.ui.Button(label='關閉', row=1)
        refresh.callback = lambda interaction: self.handle(interaction, 'refresh')
        close.callback = lambda interaction: self.handle(interaction, 'close')
        self.add_item(refresh)
        self.add_item(close)
        add_favorite_toggle(self, 1, 'divination')

    def embed(self, notice=None):
        status = self.cog.divinations.status(self.guild_id, self.owner.id)
        mastery = self.cog.divinations.mastery(self.guild_id, self.owner.id)
        affinity = self.cog.divinations.affinity_status(self.guild_id, self.owner.id)
        next_reward = affinity['next_reward']
        affinity_text = (f'**{affinity["score"]}／100**｜今日 +{affinity["today"]}／3\n'
                         f'{affinity["reward_name"]}・燃料上限 **{affinity["fuel_capacity"]:,}**')
        if next_reward:
            affinity_text += f'\n再提升 {next_reward[0] - affinity["score"]} 點：{next_reward[2]}・{next_reward[1]:,}'
        if self.showing_catalog:
            embed = discord.Embed(
                title='安安大冒險｜命運圖鑑',
                description=f'已解鎖 **{len(mastery)}／{len(CARDS)}** 張大阿爾克那。實際觸發牌效可增加共鳴。',
                color=0x6D3A8D)
            for category in ('戰鬥', '生活', '特殊'):
                lines = [f'**{card.name}**・{card_rarity(card)}・共鳴 {mastery[card_id]}' if card_id in mastery else '？？？'
                         for card_id, card in CARDS.items() if card.category == category]
                embed.add_field(name=f'{CATEGORY_ICON[category]} {category}',
                                value='\n'.join(lines), inline=True)
            embed.add_field(name='瑪格親密度', value=affinity_text, inline=False)
            return embed
        card = CARDS.get(status['card'])
        if card:
            resonance = mastery.get(status['card'], 0)
            active = (f'{CATEGORY_ICON[card.category]} **{card.name}**・{card.category}・{card_rarity(card)}\n'
                      f'{card.omen}\n{card.effect}\n'
                      f'有效至 <t:{int(status["expires_at"])}:R>｜共鳴 **{resonance}**')
            if status['summon_raid_id']:
                active += '\n女祭司的召喚機會已使用；討伐 XP 加成仍持續至牌效結束。'
        else:
            active = '目前沒有生效中的命運。'
        price = '免費' if status['next_price'] == 0 else f'{status["next_price"]:,} 金幣'
        embed = discord.Embed(
            title='安安大冒險｜瑪格的占卜室',
            description=(f'{active}\n\n今日已揭牌 **{status["draws"]}** 次；下一次為 **{price}**。\n'
                         '每次揭示三張牌並選擇一張，命運持續 6 小時；戰鬥、生活與特殊牌混在同一個牌組。'),
            color=0x6D3A8D)
        if status['offer']:
            lines = []
            for card_id in status['offer']:
                offered = CARDS[card_id]
                lines.append(f'{CATEGORY_ICON[offered.category]} **{offered.name}**・{card_rarity(offered)}｜{offered.effect}')
            embed.add_field(name='選擇你的命運', value='\n'.join(lines), inline=False)
        elif self.confirming_reveal:
            embed.add_field(name='⚠️ 請確認',
                            value='揭牌後仍會保留目前命運，直到你選定新牌；揭牌費用不退還。', inline=False)
        if notice:
            embed.insert_field_at(0, name='最新狀態', value=notice, inline=False)
        embed.add_field(name='瑪格親密度', value=affinity_text, inline=False)
        embed.set_footer(text='實際觸發牌效可使該牌取得一次共鳴。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請從自己的冒險頁進入占卜室。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('占卜室已關閉，請從 /冒險 重新進入。', ephemeral=True)
                return
            if action in ('home', 'travel'):
                await navigate(self, interaction, action)
                return
            if action == 'catalog':
                self.showing_catalog = True
            elif action == 'room':
                self.showing_catalog = False
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
            reveal_now = False
            status = self.cog.divinations.status(self.guild_id, self.owner.id)
            if action == 'reveal':
                if status['card'] and status['next_price']:
                    self.confirming_reveal = True
                else:
                    reveal_now = True
            elif action == 'keep':
                self.confirming_reveal = False
            elif action == 'reveal_confirm':
                reveal_now = self.confirming_reveal
            elif action == 'choose':
                try:
                    card_id, _expires = self.cog.divinations.choose(self.guild_id, self.owner.id, value)
                    card = CARDS[card_id]
                    notice = f'你選擇了「{card.name}」。{card.omen}\n{card.effect}'
                except CharacterError as exc:
                    notice = str(exc)
            if reveal_now:
                try:
                    _offer, cost = self.cog.divinations.reveal(self.guild_id, self.owner.id)
                    self.confirming_reveal = False
                    notice = '瑪格揭開了三張牌。' + ('今日第一次由她請客。' if cost == 0 else f'已支付 {cost:,} 金幣。')
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
