"""Private inventory transfers and shop sales with quantity confirmation."""
import asyncio
import discord

from core.rpg_character import ITEMS, CharacterError, item_value, item_text
from core.rpg_menu import add_back, navigate
from core.rpg_equipment_view import PanelSelect


class QuantityModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='確認給予數量' if panel.mode == 'give' else '確認賣出數量')
        self.panel, self.key, self.recipient = panel, panel.selected, panel.recipient
        self.revision = panel.revision
        item = ITEMS[self.key]
        label = f'{item.name}｜每件 {item_value(item) // 5} 金幣' if panel.mode == 'sell' else item.name
        self.amount = discord.ui.TextInput(label=label[:45], default='1', min_length=1, max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message('請輸入正整數數量。', ephemeral=True)
            return
        await self.panel.execute(interaction, self.key, self.recipient, amount, self.revision)


class RecipientSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder='選擇同伺服器的收件人', min_values=1, max_values=1, row=1)

    async def callback(self, interaction):
        await self.view.handle(interaction, 'recipient', self.values[0].id)


class TradeView(discord.ui.View):
    def __init__(self, cog, interaction, mode):
        super().__init__(timeout=180)
        self.cog, self.origin, self.mode = cog, interaction, mode
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.selected, self.recipient, self.page = None, None, 0
        self.closed, self.revision = False, 0
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        chars = self.cog.characters
        self.catalog = [key for key in chars.inventory(self.guild_id, self.owner.id)
                        if item_value(ITEMS[key]) > 0 and chars.available_quantity(self.guild_id, self.owner.id, key) > 0]
        self.pages = max(1, (len(self.catalog) + 9) // 10)
        self.page = min(self.page, self.pages - 1)
        if self.selected not in self.catalog:
            self.selected = None
        self.clear_items()
        keys = self.catalog[self.page * 10:(self.page + 1) * 10]
        self.add_item(PanelSelect('item', row=0, placeholder=f'選擇物品（{self.page+1}/{self.pages}）', disabled=not keys,
            options=[discord.SelectOption(label=ITEMS[key].name, value=key, default=key == self.selected,
                description=f'可用 {chars.available_quantity(self.guild_id, self.owner.id, key)} 件｜收購 {item_value(ITEMS[key])//5} 金幣／件')
                for key in keys] or [discord.SelectOption(label='沒有可用物品', value='empty')]))
        if self.mode == 'give':
            self.add_item(RecipientSelect())
        for label, action, disabled in (
            ('上一頁', 'previous', self.page == 0), ('下一頁', 'next', self.page == self.pages-1),
            ('填寫數量並確認', 'confirm', not self.selected or self.mode == 'give' and not self.recipient),
            ('重新整理', 'refresh', False), ('關閉', 'close', False)):
            button = discord.ui.Button(label=label, row=2, disabled=disabled)
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)
        add_back(self, 3)
        button = discord.ui.Button(label='返回背包' if self.mode == 'give' else '返回商店', row=3)
        async def back(interaction):
            await self.handle(interaction, 'back')
        button.callback = back
        self.add_item(button)

    def embed(self, notice=None):
        embed = discord.Embed(title='安安大冒險｜' + ('給予物品' if self.mode == 'give' else '商店收購'),
            description='選擇物品後填寫數量，送出即確認。僅能操作未穿戴的份數。\n木棒與免費補給為綁定物品。', color=0xD8AF40)
        if self.selected:
            item = ITEMS[self.selected]
            embed.add_field(name=item.name, value=f'{item_text(item)}\n價值 {item_value(item)} 金幣｜20% 收購價 {item_value(item)//5} 金幣／件', inline=False)
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
            if action == 'item' and value in self.catalog:
                self.selected = value
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
                gold = self.cog.characters.dispose(self.guild_id, self.owner.id, key, amount,
                                                  recipient if self.mode == 'give' else None)
                notice = (f'已給予 <@{recipient}> {ITEMS[key].name} ×{amount}。' if self.mode == 'give'
                          else f'已賣出 {ITEMS[key].name} ×{amount}，獲得 {gold} 金幣。')
                self.selected = None
                if self.mode == 'give':
                    sender = discord.utils.escape_markdown(getattr(self.owner, 'display_name', str(self.owner.id)))
                    guild_name = discord.utils.escape_markdown(getattr(interaction.guild, 'name', str(self.guild_id)))
                    notification = discord.Embed(title='安安大冒險｜收到道具',
                        description=f'你在 **{guild_name}** 收到 **{sender}** 贈送的道具！', color=0x8B5CF6)
                    notification.add_field(name=ITEMS[key].name, value=f'數量：{amount}\n{item_text(ITEMS[key])}', inline=False)
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
