"""Private Discord equipment panel; mutations retain the character-store checks."""
import asyncio

from core.rpg_menu import add_back, navigate

import discord

from core.rpg_character import CharacterError, ITEMS, stage_level, item_text


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
        self.available = [key for key in self.cog.characters.inventory(self.guild_id, self.owner.id)
                          if ITEMS[key].slot == kind and
                          (not ITEMS[key].job or (ITEMS[key].job == state['job'] and
                           state['level'] >= stage_level(ITEMS[key].stage, self.cog.settings)))]
        if self.item_id not in self.available:
            self.item_id = None
        self.clear_items()
        self.add_item(PanelSelect('slot', placeholder='選擇裝備欄位', row=0, options=[
            discord.SelectOption(label=slot, value=slot, default=slot == self.slot)
            for slot in state['slots']]))
        options = [discord.SelectOption(label=ITEMS[key].name, value=key,
                                        description=item_text(ITEMS[key])[:100], default=key == self.item_id)
                   for key in self.available]
        self.add_item(PanelSelect('item', placeholder='選擇要穿戴的物品' if options else '這個欄位沒有可用裝備',
                                 row=1, disabled=not options, options=options or [
                                     discord.SelectOption(label='沒有可用裝備', value='empty')]))
        for button in (self.wear, self.remove, self.refresh, self.close_panel):
            self.add_item(button)
        add_back(self, 2)
        self.wear.disabled = self.item_id is None
        self.remove.disabled = self.slot not in state['equipped']
        return state

    def embed(self, notice=None):
        embed = self.cog.character_embed(self.guild_id, self.owner)
        embed.title = '裝備與能力值｜' + embed.title
        embed.add_field(name='目前選擇', value=f'{self.slot}：{ITEMS[self.item_id].name if self.item_id else "請選擇物品"}', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='先選欄位，再選物品並按穿戴；操作後更新能力值。閒置 3 分鐘後關閉，可重新使用 /冒險 → 裝備／能力。')
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
                    self.slot, self.item_id = value, None
                elif action == 'item':
                    if value not in self.available:
                        raise CharacterError('這件裝備目前無法穿戴，請重新選擇。')
                    self.item_id = value
                elif action == 'wear':
                    if self.item_id is None:
                        raise CharacterError('請先選擇可穿戴的物品。')
                    slot_number = int(self.slot[2:]) if self.slot.startswith('飾品') else 1
                    self.cog.characters.equip(self.guild_id, self.owner.id, self.item_id, slot_number)
                    notice = f'{self.slot} 已穿戴 {ITEMS[self.item_id].name}。'
                elif action == 'remove':
                    self.cog.characters.unequip(self.guild_id, self.owner.id, self.slot)
                    notice = f'已卸下{self.slot}，物品保留在背包。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='穿戴', style=discord.ButtonStyle.success, row=2)
    async def wear(self, interaction, button):
        await self.handle(interaction, 'wear')

    @discord.ui.button(label='卸下', style=discord.ButtonStyle.secondary, row=2)
    async def remove(self, interaction, button):
        await self.handle(interaction, 'remove')

    @discord.ui.button(label='重新整理', style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction, button):
        await self.handle(interaction, 'refresh')

    @discord.ui.button(label='關閉', style=discord.ButtonStyle.secondary, row=2)
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
