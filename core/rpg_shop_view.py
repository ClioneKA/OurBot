"""Currency-specific shops with explicit, transactional purchases."""
import asyncio

from core.rpg_menu import add_back, navigate

import discord

from core.rpg_character import (BALLOON_PAINTING_PROOF_COST, CharacterError, ITEMS,
                                item_text, stage_level)
from core.rpg_equipment_view import PanelSelect
from core.rpg_slot_expansions import EXPANSIONS


class ShopView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.item_id = None
        self.currency = 'gold'
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
        self.catalog = ([key for key, item in ITEMS.items()
                         if item.job == state['job'] and item.price > 0] + ['expansion:recipe']
                        if self.currency == 'gold' else ['painting:balloon', 'expansion:loadout'])
        self.expansion_status = {
            key: self.cog.characters.expansions.status(self.guild_id, self.owner.id, key)
            for key in EXPANSIONS}
        if self.item_id not in self.catalog:
            self.item_id = None
        owned = set(self.cog.characters.inventory(self.guild_id, self.owner.id))
        proofs = self.cog.characters.inventory_counts(self.guild_id, self.owner.id).get('proof:raid', 0)
        self.clear_items()
        self.add_item(PanelSelect('currency', row=0, placeholder='選擇貨幣分類', options=[
            discord.SelectOption(label=label, value=key, default=key == self.currency)
            for key, label in (('gold', '金幣商店'), ('proof', '討伐之證兌換'))]))
        self.add_item(PanelSelect('item', row=1, placeholder='選擇商品',
            disabled=not self.catalog, options=[discord.SelectOption(
                label=EXPANSIONS[key][0] if key in EXPANSIONS else ITEMS[key].name, value=key,
                description=(self.expansion_description(key) if key in EXPANSIONS else
                            f'{BALLOON_PAINTING_PROOF_COST} 討伐之證｜繪畫之影路線'
                            if key == 'painting:balloon' else
                            f'{ITEMS[key].price:,} 金幣｜Lv.{stage_level(ITEMS[key].stage, self.cog.settings)}')
                            + ('｜已持有' if key != 'painting:balloon' and key in owned else ''),
                default=key == self.item_id) for key in self.catalog]
            or [discord.SelectOption(label='尚無商品', value='empty')]))
        item = ITEMS.get(self.item_id)
        balloon = self.item_id == 'painting:balloon'
        self.buy_button.label = (f'兌換（{BALLOON_PAINTING_PROOF_COST} 證）' if balloon else
                                 f'購買（{item.price:,} 金幣）' if item else '購買')
        if self.item_id in EXPANSIONS:
            status = self.expansion_status[self.item_id]
            price = status['price']
            self.buy_button.label = (f'購買（{price:,} {status["currency"]}）'
                                     if price is not None else '已售完（5/5）')
            balance = proofs if self.item_id == 'expansion:loadout' else self.cog.store.gold(
                self.guild_id, self.owner.id)
            self.buy_button.disabled = price is None or balance < price
        elif not item:
            self.buy_button.disabled = True
        elif balloon:
            self.buy_button.disabled = proofs < BALLOON_PAINTING_PROOF_COST
        else:
            self.buy_button.disabled = (
                self.item_id in owned
                or state['level'] < stage_level(item.stage, self.cog.settings)
                or self.cog.store.gold(self.guild_id, self.owner.id) < item.price)
        for button in (self.buy_button, self.refresh, self.close_panel, self.sell_button):
            self.add_item(button)
        add_back(self, 2)

    def expansion_description(self, key):
        status = self.expansion_status[key]
        price = status['price']
        cost = f'{price:,} {status["currency"]}' if price is not None else '已售完'
        return f'{cost}｜已購 {status["purchased"]}/{status["limit"]}｜永久 +1 格'

    def embed(self, notice=None):
        gold = self.cog.store.gold(self.guild_id, self.owner.id)
        proofs = self.cog.characters.inventory_counts(self.guild_id, self.owner.id).get('proof:raid', 0)
        embed = discord.Embed(title='金幣商店' if self.currency == 'gold' else '討伐之證兌換',
            description=(f'持有金幣：**{gold:,}**｜討伐之證：**{proofs:,}**\n'
                         '選擇商品查看內容，再按購買或兌換。'), color=0xD8AF40)
        if self.item_id in EXPANSIONS:
            status = self.expansion_status[self.item_id]
            prices = ' → '.join(f'{price:,}' for price in EXPANSIONS[self.item_id][2])
            embed.add_field(name=status['name'], value=(
                f'{self.expansion_description(self.item_id)}\n'
                f'目前 {3 + status["purchased"]} 格／最多 8 格；購買後立即生效。\n'
                f'五次價格（{status["currency"]}）：{prices}'), inline=False)
        elif self.item_id:
            item = ITEMS[self.item_id]
            price = (f'{BALLOON_PAINTING_PROOF_COST} 討伐之證' if self.item_id == 'painting:balloon'
                     else f'{item.price:,} 金幣｜{item.job} Lv.{stage_level(item.stage, self.cog.settings)}')
            embed.add_field(name=item.name, value=f'{price}\n{item_text(item)}', inline=False)
        else:
            if self.currency == 'gold':
                embed.add_field(name='職業裝備', value='無前綴：500 金幣／件\n老練：1,500 金幣／件\n精銳：4,000 金幣／件', inline=False)
            else:
                embed.add_field(name=ITEMS['painting:balloon'].name,
                                value=f'{BALLOON_PAINTING_PROOF_COST} 討伐之證｜繪畫之影路線', inline=False)
            embed.add_field(name='永久欄位擴充', value='\n'.join(
                f'{EXPANSIONS[key][0]}：{self.expansion_description(key)}'
                for key in self.catalog if key in EXPANSIONS), inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='裝備需達等級且未持有才能購買；擴充每種限購 5 次，每次價格提高並永久增加 1 格。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 商店 開啟自己的商店。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('商店已關閉，請重新使用 /冒險 → 商店。', ephemeral=True)
                return
            if action == 'home':
                await navigate(self, interaction)
                return
            if action == 'sell':
                await navigate(self, interaction, 'sell')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='商店已關閉。', embed=None, view=None)
                return
            quoted = self.expansion_status.get(self.item_id)
            self.rebuild()
            notice = None
            try:
                if action == 'currency':
                    if value not in ('gold', 'proof'):
                        raise CharacterError('無效的貨幣分類。')
                    self.currency = value
                    self.item_id = None
                elif action == 'item':
                    if value not in self.catalog:
                        raise CharacterError('這項商品不在目前分類或不適合目前職業，請重新選擇。')
                    self.item_id = value
                elif action == 'buy':
                    if self.item_id in EXPANSIONS:
                        status = self.cog.characters.expansions.buy(
                            self.guild_id, self.owner.id, self.item_id,
                            expected_purchased=quoted['purchased'] if quoted else None)
                        notice = (f'已花費 {status["price"]:,} {status["currency"]}購買'
                                  f'{status["name"]}，永久增加 1 格，現在共 '
                                  f'{4 + status["purchased"]} 格。')
                    elif self.item_id == 'painting:balloon':
                        item = self.cog.characters.exchange_balloon_painting(
                            self.guild_id, self.owner.id)
                        notice = (f'已使用 {BALLOON_PAINTING_PROOF_COST} 個討伐之證兌換 '
                                  f'{item.name}，已放入背包。')
                    else:
                        item = self.cog.characters.buy(self.guild_id, self.owner.id, self.item_id)
                        notice = f'已花費 {item.price:,} 金幣購入 {item.name}，已放入背包。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='購買', style=discord.ButtonStyle.success, row=2)
    async def buy_button(self, interaction, button):
        await self.handle(interaction, 'buy')

    @discord.ui.button(label='賣出物品', row=2)
    async def sell_button(self, interaction, button):
        await self.handle(interaction, 'sell')

    @discord.ui.button(label='重新整理', row=2)
    async def refresh(self, interaction, button):
        await self.handle(interaction, 'refresh')

    @discord.ui.button(label='關閉', row=2)
    async def close_panel(self, interaction, button):
        await self.handle(interaction, 'close')

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='商店已逾時，請重新使用 /冒險 → 商店。', view=None)
            except discord.HTTPException:
                pass
