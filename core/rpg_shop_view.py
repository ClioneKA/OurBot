"""Gold shop with explicit purchase button and transactional inventory delivery."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, item_text, stage_level
from core.rpg_equipment_view import PanelSelect


class ShopView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.item_id = None
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
        self.catalog = [key for key, item in ITEMS.items() if item.job == state['job'] and item.price > 0]
        if self.item_id not in self.catalog:
            self.item_id = None
        owned = set(self.cog.characters.inventory(self.guild_id, self.owner.id))
        self.clear_items()
        self.add_item(PanelSelect('item', row=0, placeholder='選擇商品' if self.catalog else 'Lv.10 轉職後可購買職業裝備',
            disabled=not self.catalog, options=[discord.SelectOption(label=ITEMS[key].name, value=key,
                description=f'{ITEMS[key].price:,} 金幣｜Lv.{stage_level(ITEMS[key].stage, self.cog.settings)}'
                            + ('｜已持有' if key in owned else ''), default=key == self.item_id) for key in self.catalog]
            or [discord.SelectOption(label='尚無商品', value='empty')]))
        item = ITEMS.get(self.item_id)
        self.buy_button.label = f'購買（{item.price:,} 金幣）' if item else '購買'
        self.buy_button.disabled = (not item or self.item_id in owned or
            state['level'] < stage_level(item.stage, self.cog.settings) or
            self.cog.store.gold(self.guild_id, self.owner.id) < item.price)
        for button in (self.buy_button, self.refresh, self.close_panel):
            self.add_item(button)

    def embed(self, notice=None):
        gold = self.cog.store.gold(self.guild_id, self.owner.id)
        embed = discord.Embed(title='裝備商店', description=f'持有金幣：**{gold:,}**\n選擇商品查看能力，再按購買。', color=0xD8AF40)
        if self.item_id:
            item = ITEMS[self.item_id]
            embed.add_field(name=item.name, value=f'{item.price:,} 金幣｜{item.job} Lv.{stage_level(item.stage, self.cog.settings)}\n{item_text(item)}', inline=False)
        else:
            embed.add_field(name='商品', value='無前綴：500 金幣／件\n老練：1,500 金幣／件\n精銳：4,000 金幣／件', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='需達到裝備等級才能購買；購入後放入背包，使用 /裝備 穿戴。已持有物品不能重複購買。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /商店 開啟自己的商店。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('商店已關閉，請重新使用 /商店。', ephemeral=True)
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='商店已關閉。', embed=None, view=None)
                return
            self.rebuild()
            notice = None
            try:
                if action == 'item':
                    if value not in self.catalog:
                        raise CharacterError('這件裝備不適合目前職業，請重新選擇。')
                    self.item_id = value
                elif action == 'buy':
                    item = self.cog.characters.buy(self.guild_id, self.owner.id, self.item_id)
                    notice = f'已花費 {item.price:,} 金幣購入 {item.name}，已放入背包。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='購買', style=discord.ButtonStyle.success, row=1)
    async def buy_button(self, interaction, button):
        await self.handle(interaction, 'buy')

    @discord.ui.button(label='重新整理', row=1)
    async def refresh(self, interaction, button):
        await self.handle(interaction, 'refresh')

    @discord.ui.button(label='關閉', row=1)
    async def close_panel(self, interaction, button):
        await self.handle(interaction, 'close')

    async def on_timeout(self):
        async with self.lock:
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='商店已逾時，請重新使用 /商店。', view=None)
            except discord.HTTPException:
                pass
