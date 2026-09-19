"""Private personal-storage panel for moving RPG items in and out of the backpack."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, inventory_entry_label, item_text
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import BACKPACK_CATEGORIES, navigate


STORAGE_CATEGORIES = tuple(dict.fromkeys((*BACKPACK_CATEGORIES,
                                         *(item.category for item in ITEMS.values()))))


class StorageQuantityModal(discord.ui.Modal):
    def __init__(self, panel):
        action = '存入' if panel.mode == 'deposit' else '取出'
        super().__init__(title=f'確認{action}數量')
        self.panel = panel
        self.reference = panel.selected
        self.revision = panel.revision
        entry = panel.entries[self.reference]
        maximum = entry.quantity
        self.amount = discord.ui.TextInput(
            label=f'{entry.item.name}｜最多 {maximum}'[:45], default='1',
            min_length=1, max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            amount = int(self.amount.value)
            if amount < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('請輸入正整數數量。', ephemeral=True)
            return
        await self.panel.execute(interaction, self.reference, amount, self.revision)


class StorageView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.mode, self.category, self.page = 'deposit', '全部', 0
        self.selected = None
        self.closed, self.revision = False, 0
        self.lock = asyncio.Lock()
        self.rebuild()

    def _button(self, label, action, row, *, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)

        async def callback(interaction):
            await self.handle(interaction, action)

        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        chars = self.cog.characters
        source = (chars.inventory_entries(self.guild_id, self.owner.id)
                  if self.mode == 'deposit' else chars.storage_entries(self.guild_id, self.owner.id))
        try:
            protected = (chars.loadout_equipment_ids(self.guild_id, self.owner.id)
                         if self.mode == 'deposit' else set())
        except CharacterError:
            # The service rechecks before moving anything and will explain a
            # corrupt preset; keep the warehouse itself available for other items.
            protected = set()
        self.entries = {entry.reference: entry for entry in source
                        if (self.category == '全部' or entry.item.category == self.category)
                        and (self.mode == 'retrieve' or
                             entry.instance_id not in protected and
                             chars.available_quantity(self.guild_id, self.owner.id, entry.reference) > 0)}
        self.catalog = list(self.entries)
        self.pages = max(1, (len(self.catalog) + 9) // 10)
        self.page = min(self.page, self.pages - 1)
        if self.selected not in self.entries:
            self.selected = None

        self.clear_items()
        self.add_item(PanelSelect('category', row=0, placeholder='選擇物品分類', options=[
            discord.SelectOption(label=category, value=category, default=category == self.category)
            for category in STORAGE_CATEGORIES]))
        keys = self.catalog[self.page * 10:(self.page + 1) * 10]
        self.add_item(PanelSelect('item', row=1,
            placeholder=f'{self.category}｜選擇物品（{self.page + 1}/{self.pages}）',
            disabled=not keys, options=[
                discord.SelectOption(
                    label=inventory_entry_label(self.entries[key], set()), value=key,
                    default=key == self.selected,
                    description=('獨立裝備｜一次一件' if self.entries[key].instance_id
                                 else f'數量 {self.entries[key].quantity}'))
                for key in keys] or [discord.SelectOption(label='此分類沒有物品', value='empty')]))
        self._button('存入', 'deposit', 2, disabled=self.mode == 'deposit',
                     style=discord.ButtonStyle.primary)
        self._button('取出', 'retrieve', 2, disabled=self.mode == 'retrieve',
                     style=discord.ButtonStyle.primary)
        self._button('上一頁', 'previous', 3, disabled=self.page == 0)
        self._button('下一頁', 'next', 3, disabled=self.page == self.pages - 1)
        self._button('填寫數量並確認', 'confirm', 3, disabled=not self.selected,
                     style=discord.ButtonStyle.success)
        self._button('返回背包', 'back', 4)
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)

    def embed(self, notice=None):
        action = '存入倉庫' if self.mode == 'deposit' else '從倉庫取出'
        embed = discord.Embed(
            title=f'安安大冒險｜倉庫・{action}',
            description='倉庫依伺服器與玩家分開保存。一般物品可指定數量；獨立裝備每次一件，並保留編號、染色、刺繡與結晶。\n'
                        '正在穿戴或已保存在出戰配置中的裝備不會列入可存物品。',
            color=0x64748B)
        embed.add_field(name='物品分類',
                        value=f'{self.category}｜{len(self.catalog)} 筆｜第 {self.page + 1}/{self.pages} 頁',
                        inline=False)
        if self.selected:
            entry = self.entries[self.selected]
            embed.add_field(name=inventory_entry_label(entry, set()),
                            value=f'數量：{entry.quantity}\n{item_text(entry.item)}', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='閒置 3 分鐘後關閉；使用 /冒險 → 背包 → 倉庫重新開啟。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 開啟自己的倉庫。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'back':
                await navigate(self, interaction, 'backpack')
                return
            if action == 'close':
                await interaction.response.edit_message(content='倉庫已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            self.rebuild()
            if action == 'confirm' and self.selected:
                await interaction.response.send_modal(StorageQuantityModal(self))
                return
            self.revision += 1
            if action in ('deposit', 'retrieve'):
                self.mode, self.page, self.selected = action, 0, None
            elif action == 'category' and value in STORAGE_CATEGORIES:
                self.category, self.page, self.selected = value, 0, None
            elif action == 'item' and value in self.entries:
                self.selected = value
            elif action in ('next', 'previous'):
                self.page = max(0, min(self.pages - 1,
                                       self.page + (1 if action == 'next' else -1)))
                self.selected = None
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

    async def execute(self, interaction, reference, amount, revision):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished() or revision != self.revision:
                await interaction.response.send_message('設定已變更或操作已完成，請重新選擇物品。', ephemeral=True)
                return
            await interaction.response.defer()
            self.revision += 1
            try:
                operation = (self.cog.characters.store_item if self.mode == 'deposit'
                             else self.cog.characters.retrieve_item)
                item, actual = operation(self.guild_id, self.owner.id, reference, amount)
                verb = '存入倉庫' if self.mode == 'deposit' else '取回背包'
                notice = f'已將 {item.name} ×{actual} {verb}。'
                self.selected = None
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await self.origin.edit_original_response(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(
                    content='倉庫面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
