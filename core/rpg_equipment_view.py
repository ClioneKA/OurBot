"""Private Discord equipment panel; mutations retain the character-store checks."""
import asyncio

from core.rpg_menu import add_back, navigate

import discord

from core.rpg_character import CharacterError, item_level, item_text


PAGE_SIZE = 25


class PanelSelect(discord.ui.Select):
    def __init__(self, action, **kwargs):
        super().__init__(**kwargs)
        self.action = action

    async def callback(self, interaction):
        await self.view.handle(interaction, self.action, self.values[0])


class EquipmentView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog = cog
        self.origin = interaction
        self.owner = interaction.user
        self.guild_id = interaction.guild_id
        self.slot = '武器'
        self.item_id = None
        self.item_page = 0
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner.id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message('這是其他冒險者的面板，請使用 /冒險 → 裝備／能力 開啟自己的面板。', ephemeral=True)
            return False
        return True

    def rebuild(self):
        state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
        if self.slot not in state['slots']:
            self.slot = '武器'
        kind = '飾品' if self.slot.startswith('飾品') else self.slot
        instances = self.cog.characters.equipment_instances(self.guild_id, self.owner.id)
        self.available_items = {instance.token: self.cog.characters.resolved_item(instance)
                                for instance in instances}
        all_available = [token for token, item in self.available_items.items()
                         if item.slot == kind and
                         (not item.job or item.job == state['job']) and
                         state['level'] >= item_level(item, self.cog.settings)]
        self.item_pages = max(1, (len(all_available) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.item_page = min(self.item_page, self.item_pages - 1)
        self.available = all_available[self.item_page * PAGE_SIZE:(self.item_page + 1) * PAGE_SIZE]
        if self.item_id not in self.available:
            self.item_id = None
        self.clear_items()
        self.add_item(PanelSelect('slot', placeholder='選擇裝備欄位', row=0, options=[
            discord.SelectOption(label=slot, value=slot, default=slot == self.slot)
            for slot in state['slots']]))
        options = [discord.SelectOption(label=f'{self.available_items[token].name} #{token.split(":")[1]}', value=token,
                                        description=item_text(self.available_items[token])[:100], default=token == self.item_id)
                   for token in self.available]
        self.add_item(PanelSelect('item', placeholder='選擇要穿戴的物品' if options else '這個欄位沒有可用裝備',
                                 row=1, disabled=not options, options=options or [
                                     discord.SelectOption(label='沒有可用裝備', value='empty')]))
        buttons = [self.wear, self.remove]
        if self.item_pages > 1:
            for label, action, disabled in (
                    ('裝備上一頁', 'item_previous', self.item_page == 0),
                    ('裝備下一頁', 'item_next', self.item_page == self.item_pages - 1)):
                button = discord.ui.Button(label=label, row=3, disabled=disabled)
                async def callback(interaction, action=action):
                    await self.handle(interaction, action)
                button.callback = callback
                buttons.append(button)
        buttons.extend((self.provisions, self.refresh, self.close_panel))
        for button in buttons:
            self.add_item(button)
        add_back(self, 4)
        self.wear.disabled = self.item_id is None
        self.remove.disabled = self.slot not in state['equipped']
        return state

    def embed(self, notice=None):
        embed = self.cog.character_embed(self.guild_id, self.owner)
        embed.title = '裝備與能力值｜' + embed.title
        selected = self.available_items.get(self.item_id)
        embed.add_field(name='目前選擇', value=f'{self.slot}：{selected.name if selected else "請選擇物品"}', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='先選欄位與物品；裝備染色與飾品刺繡請前往遠野漢娜的裁縫所。閒置 3 分鐘後關閉，可重新使用 /冒險 → 裝備／能力。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險 → 裝備／能力。', ephemeral=True)
                return
            notice = None
            if action == 'home':
                await navigate(self, interaction)
                return
            if action == 'provision_loadout':
                await navigate(self, interaction, 'provision_loadout')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='裝備面板已關閉。', embed=None, view=None)
                return
            # Re-read state so another panel, a job change, or levelling cannot bypass rules.
            self.rebuild()
            try:
                if action == 'slot':
                    state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
                    if value not in state['slots']:
                        raise CharacterError('此欄位尚未解鎖。')
                    self.slot, self.item_id, self.item_page = value, None, 0
                elif action in ('item_previous', 'item_next'):
                    self.item_page = max(0, min(self.item_pages - 1,
                        self.item_page + (1 if action == 'item_next' else -1)))
                    self.item_id = None
                elif action == 'item':
                    # Accept a definition id as a short compatibility path for
                    # older component payloads; new menus always carry a unique
                    # instance token.
                    if value not in self.available:
                        value = next((token for token in self.available
                                      if self.cog.characters.instance_item_id(
                                          self.cog.characters.get_instance(
                                              self.guild_id, self.owner.id, token)) == value), None)
                    if value not in self.available:
                        raise CharacterError('這件裝備目前無法穿戴，請重新選擇。')
                    self.item_id = value
                elif action == 'wear':
                    if self.item_id is None:
                        raise CharacterError('請先選擇可穿戴的物品。')
                    slot_number = int(self.slot[2:]) if self.slot.startswith('飾品') else 1
                    self.cog.characters.equip(self.guild_id, self.owner.id, self.item_id, slot_number)
                    notice = f'{self.slot} 已穿戴 {self.available_items[self.item_id].name}。'
                elif action == 'remove':
                    self.cog.characters.unequip(self.guild_id, self.owner.id, self.slot)
                    notice = f'已卸下{self.slot}，物品保留在背包。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='穿戴', style=discord.ButtonStyle.success, row=3)
    async def wear(self, interaction, button):
        await self.handle(interaction, 'wear')

    @discord.ui.button(label='卸下', style=discord.ButtonStyle.secondary, row=3)
    async def remove(self, interaction, button):
        await self.handle(interaction, 'remove')

    @discord.ui.button(label='討伐補給', style=discord.ButtonStyle.secondary, row=4)
    async def provisions(self, interaction, button):
        await self.handle(interaction, 'provision_loadout')

    @discord.ui.button(label='重新整理', style=discord.ButtonStyle.secondary, row=4)
    async def refresh(self, interaction, button):
        await self.handle(interaction, 'refresh')

    @discord.ui.button(label='關閉', style=discord.ButtonStyle.secondary, row=4)
    async def close_panel(self, interaction, button):
        await self.handle(interaction, 'close')

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='裝備面板已逾時，請重新使用 /冒險 → 裝備／能力。', view=None)
            except discord.HTTPException:
                pass  # The user may already have dismissed the private message.
