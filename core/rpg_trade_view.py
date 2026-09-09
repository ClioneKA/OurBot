"""Private inventory transfers and shop sales with quantity confirmation."""
import asyncio
import discord

from core.rpg_character import CharacterError, ITEMS, item_sell_price, item_sellable, item_text
from core.rpg_menu import BACKPACK_CATEGORIES, add_back, navigate
from core.rpg_equipment_view import PanelSelect


TRADE_CATEGORIES = tuple(dict.fromkeys((*BACKPACK_CATEGORIES,
                                      *(item.category for item in ITEMS.values()))))


class QuantityModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='確認給予數量' if panel.mode == 'give' else '確認賣出數量')
        self.panel, self.key, self.recipient = panel, panel.selected, panel.recipient
        self.revision = panel.revision
        item = panel.entries[self.key].item
        label = f'{item.name}｜每件 {item_sell_price(item)} 金幣' if panel.mode == 'sell' else item.name
        self.amount = discord.ui.TextInput(label=label[:45], default='1', min_length=1, max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message('請輸入正整數數量。', ephemeral=True)
            return
        if self.panel.entries[self.key].instance_id and amount != 1:
            await interaction.response.send_message('獨立裝備一次只能操作一件。', ephemeral=True)
            return
        await self.panel.execute(interaction, self.key, self.recipient, amount, self.revision)


class RecipientSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder='選擇同伺服器的收件人', min_values=1, max_values=1, row=2)

    async def callback(self, interaction):
        await self.view.handle(interaction, 'recipient', self.values[0].id)


class TradeView(discord.ui.View):
    def __init__(self, cog, interaction, mode):
        super().__init__(timeout=180)
        self.cog, self.origin, self.mode = cog, interaction, mode
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.selected, self.recipient, self.page = None, None, 0
        self.category = '全部'
        self.closed, self.revision = False, 0
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        chars = self.cog.characters
        self.entries = {entry.reference: entry for entry in chars.inventory_entries(self.guild_id, self.owner.id)}
        self.catalog = [reference for reference, entry in self.entries.items()
                        if (item_sellable(entry.item) if self.mode == 'sell' else entry.item.transferable)
                        and (self.category == '全部' or entry.item.category == self.category)
                        and chars.available_quantity(self.guild_id, self.owner.id, reference) > 0]
        self.pages = max(1, (len(self.catalog) + 9) // 10)
        self.page = min(self.page, self.pages - 1)
        if self.selected not in self.catalog:
            self.selected = None
        self.clear_items()
        self.add_item(PanelSelect('category', row=0, placeholder='選擇物品分類', options=[
            discord.SelectOption(label=category, value=category, default=category == self.category)
            for category in TRADE_CATEGORIES]))
        keys = self.catalog[self.page * 10:(self.page + 1) * 10]
        self.add_item(PanelSelect('item', row=1, placeholder=f'{self.category}｜選擇物品（{self.page+1}/{self.pages}）', disabled=not keys,
            options=[discord.SelectOption(
                label=(f'{self.entries[key].item.name} #{self.entries[key].instance_id}'
                       if self.entries[key].instance_id else self.entries[key].item.name),
                value=key, default=key == self.selected,
                description=(('獨立裝備｜一次一件' if self.entries[key].instance_id else
                              f'可用 {chars.available_quantity(self.guild_id, self.owner.id, key)} 件')
                             + (f'｜收購 {item_sell_price(self.entries[key].item)} 金幣／件'
                                if self.mode == 'sell' else '')))
                for key in keys] or [discord.SelectOption(label='此分類沒有可用物品', value='empty')]))
        if self.mode == 'give':
            self.add_item(RecipientSelect())
        for label, action, disabled in (
            ('上一頁', 'previous', self.page == 0), ('下一頁', 'next', self.page == self.pages-1),
            ('填寫數量並確認', 'confirm', not self.selected or self.mode == 'give' and not self.recipient),
            ('重新整理', 'refresh', False), ('關閉', 'close', False)):
            button = discord.ui.Button(label=label, row=3, disabled=disabled)
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)
        add_back(self, 4)
        button = discord.ui.Button(label='返回背包' if self.mode == 'give' else '返回商店', row=4)
        async def back(interaction):
            await self.handle(interaction, 'back')
        button.callback = back
        self.add_item(button)

    def embed(self, notice=None):
        embed = discord.Embed(title='安安大冒險｜' + ('給予物品' if self.mode == 'give' else '商店收購'),
            description='先選擇分類與物品，再填寫數量，送出即確認。僅能操作未穿戴的份數。\n'
                        '木棒與免費補給不可給予，但可用 0 金幣出售；釣竿不可給予或出售。', color=0xD8AF40)
        embed.add_field(name='物品分類', value=f'{self.category}｜{len(self.catalog)} 筆｜第 {self.page + 1}/{self.pages} 頁', inline=False)
        if self.selected:
            item = self.entries[self.selected].item
            value = item_text(item)
            if self.mode == 'sell':
                value += f'\n收購價 {item_sell_price(item)} 金幣／件'
            embed.add_field(name=item.name, value=value, inline=False)
        if self.mode == 'give':
            embed.add_field(name='收件人', value=f'<@{self.recipient}>' if self.recipient else '尚未選擇')
        else:
            embed.add_field(name='持有金幣', value=str(self.cog.store.gold(self.guild_id, self.owner.id)))
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='閒置 3 分鐘後關閉；使用 /冒險 重新開啟。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 開啟自己的面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action in ('home', 'back'):
                await navigate(self, interaction, 'home' if action == 'home' else 'backpack' if self.mode == 'give' else 'shop')
                return
            if action == 'close':
                await interaction.response.edit_message(content='面板已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            self.rebuild()
            if action == 'confirm' and self.selected and (self.mode == 'sell' or self.recipient):
                await interaction.response.send_modal(QuantityModal(self))
                return
            self.revision += 1
            if action == 'category' and value in TRADE_CATEGORIES:
                self.category, self.page, self.selected = value, 0, None
            elif action == 'item' and value in self.catalog:
                self.selected = value
            elif action == 'item':
                # Compatibility for component payloads created before instance ids.
                self.selected = next((reference for reference in self.catalog
                                      if self.entries[reference].item_id == value), None)
            elif action == 'recipient':
                self.recipient = value
            elif action in ('next', 'previous'):
                self.page = max(0, min(self.pages-1, self.page + (1 if action == 'next' else -1)))
                self.selected = None
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    async def execute(self, interaction, key, recipient, amount, revision):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished() or revision != self.revision:
                await interaction.response.send_message('設定已變更或操作已完成，請重新選擇物品。', ephemeral=True)
                return
            await interaction.response.defer()
            self.revision += 1
            try:
                if self.mode == 'give':
                    if not recipient or recipient == self.owner.id:
                        raise CharacterError('請選擇其他伺服器成員。')
                    try:
                        member = await interaction.guild.fetch_member(recipient)
                    except discord.HTTPException:
                        raise CharacterError('無法確認收件人仍在伺服器，請重新選擇。')
                    if member.bot:
                        raise CharacterError('不能給予機器人。')
                    if not self.cog.store.has_player(self.guild_id, recipient):
                        raise CharacterError('對方尚未接受邀請，不能接收冒險道具。')
                item = self.cog.characters.item_for_reference(self.guild_id, self.owner.id, key)
                gold = self.cog.characters.dispose(self.guild_id, self.owner.id, key, amount,
                                                  recipient if self.mode == 'give' else None)
                notice = (f'已給予 <@{recipient}> {item.name} ×{amount}。' if self.mode == 'give'
                          else f'已賣出 {item.name} ×{amount}，獲得 {gold} 金幣。')
                self.selected = None
                if self.mode == 'give':
                    sender = discord.utils.escape_markdown(getattr(self.owner, 'display_name', str(self.owner.id)))
                    guild_name = discord.utils.escape_markdown(getattr(interaction.guild, 'name', str(self.guild_id)))
                    notification = discord.Embed(title='安安大冒險｜收到道具',
                        description=f'你在 **{guild_name}** 收到 **{sender}** 贈送的道具！', color=0x8B5CF6)
                    notification.add_field(name=item.name, value=f'數量：{amount}\n{item_text(item)}', inline=False)
                    notification.set_footer(text='道具已放入該伺服器的背包，使用 /冒險 → 背包 查看。')
                    try:
                        await asyncio.wait_for(member.send(embed=notification,
                                                           allowed_mentions=discord.AllowedMentions.none()), timeout=20)
                        notice += '\n已私訊通知對方。'
                    except (discord.HTTPException, asyncio.TimeoutError):
                        notice += '\n道具已入帳，但私訊通知未能送達；對方可能關閉了私訊。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await self.origin.edit_original_response(embed=self.embed(notice), view=self,
                                                     allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
