"""Pigment-crystal services inside Tono Hanna's tailor shop."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, item_text
from core.rpg_crystals import (CRYSTAL_REMOVAL_PRICE, CRYSTAL_TYPES,
                               QUALITY_SELL_PRICES, crystal_affix_name,
                               crystal_effect_text)
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate


PAGE_SIZE = 20
MODE_LABELS = {
    'inventory': '結晶一覽',
    'socket': '鑲嵌',
    'remove': '拆除',
    'sell': '出售',
    'give': '給予',
}


class CrystalRecipientSelect(discord.ui.UserSelect):
    async def callback(self, interaction):
        await self.view.handle(interaction, 'recipient', self.values[0])


class CrystalTailorView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.mode, self.page = 'inventory', 0
        self.selected_crystal = self.selected_equipment = self.selected_slot = None
        self.recipient = None
        self.closed, self.lock = False, asyncio.Lock()
        self.rebuild()

    def _button(self, label, action, row, *, disabled=False,
                style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def _crystal_description(self, crystal):
        job = f'｜{crystal.job}限定' if crystal.job else ''
        state = (f'裝備 #{crystal.equipment_instance_id}'
                 if crystal.equipment_instance_id else '未鑲嵌')
        return f'{crystal_affix_name(crystal)}｜{state}{job}'[:100]

    def _equipment_entries(self):
        return [entry for entry in self.cog.characters.inventory_entries(
            self.guild_id, self.owner.id)
            if entry.instance_id and entry.item.crystal_slots]

    def rebuild(self):
        store = self.cog.painted_maze.crystals
        all_crystals = store.inventory(self.guild_id, self.owner.id)
        unsocketed = [crystal for crystal in all_crystals
                      if crystal.equipment_instance_id is None]
        equipment = self._equipment_entries()
        if self.mode == 'inventory':
            primary = all_crystals
        elif self.mode in ('socket', 'sell', 'give'):
            primary = unsocketed
        else:
            socketed_ids = {crystal.equipment_instance_id for crystal in all_crystals
                            if crystal.equipment_instance_id is not None}
            primary = [entry for entry in equipment if entry.instance_id in socketed_ids]

        pages = max(1, (len(primary) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        visible = primary[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        crystal_ids = {crystal.instance_id for crystal in all_crystals}
        equipment_refs = {entry.reference for entry in equipment}
        if self.selected_crystal not in crystal_ids:
            self.selected_crystal = None
        if self.selected_equipment not in equipment_refs:
            self.selected_equipment = None

        self.crystals = {crystal.instance_id: crystal for crystal in all_crystals}
        self.equipment = {entry.reference: entry for entry in equipment}
        self.pages = pages
        self.clear_items()
        for mode, label in MODE_LABELS.items():
            self._button(label, f'mode:{mode}', 0,
                         style=(discord.ButtonStyle.primary if mode == self.mode
                                else discord.ButtonStyle.secondary))

        if self.mode == 'remove':
            options = [discord.SelectOption(
                label=f'{entry.item.name} #{entry.instance_id}', value=entry.reference,
                description=item_text(entry.item)[:100],
                default=entry.reference == self.selected_equipment) for entry in visible]
            self.add_item(PanelSelect(
                'equipment', row=1, placeholder='選擇要拆除結晶的裝備' if options else '沒有已鑲嵌結晶的裝備',
                disabled=not options,
                options=options or [discord.SelectOption(label='沒有可選裝備', value='empty')]))
        else:
            options = [discord.SelectOption(
                label=f'#{crystal.instance_id} {crystal.name}', value=str(crystal.instance_id),
                description=self._crystal_description(crystal),
                default=crystal.instance_id == self.selected_crystal) for crystal in visible]
            self.add_item(PanelSelect(
                'crystal', row=1, placeholder='選擇顏料結晶' if options else '目前沒有可用的顏料結晶',
                disabled=not options,
                options=options or [discord.SelectOption(label='沒有可選結晶', value='empty')]))

        if self.mode == 'socket':
            crystal = self.crystals.get(self.selected_crystal)
            compatible = [entry for entry in equipment
                          if (not crystal or crystal.crystal_type in entry.item.crystal_slots)
                          and (not crystal or crystal.crystal_type != 'source'
                               or crystal.job == entry.item.job)]
            equipment_options = [discord.SelectOption(
                label=f'{entry.item.name} #{entry.instance_id}', value=entry.reference,
                description=item_text(entry.item)[:100],
                default=entry.reference == self.selected_equipment)
                for entry in compatible[:25]]
            self.add_item(PanelSelect(
                'equipment', row=2, placeholder='選擇要鑲嵌的 T60 菁英裝備' if equipment_options
                else '沒有相容的 T60 菁英裝備', disabled=not equipment_options,
                options=equipment_options or [discord.SelectOption(label='沒有相容裝備', value='empty')]))
        elif self.mode == 'remove':
            selected_id = (self.equipment[self.selected_equipment].instance_id
                           if self.selected_equipment in self.equipment else None)
            socketed = [crystal for crystal in all_crystals
                        if crystal.equipment_instance_id == selected_id]
            if self.selected_slot not in {crystal.crystal_type for crystal in socketed}:
                self.selected_slot = None
            slot_options = [discord.SelectOption(
                label=CRYSTAL_TYPES[crystal.crystal_type], value=crystal.crystal_type,
                description=f'#{crystal.instance_id} {crystal_affix_name(crystal)}',
                default=crystal.crystal_type == self.selected_slot) for crystal in socketed]
            self.add_item(PanelSelect(
                'slot', row=2, placeholder='選擇要拆除的槽位' if slot_options else '請先選擇裝備',
                disabled=not slot_options,
                options=slot_options or [discord.SelectOption(label='沒有可拆除槽位', value='empty')]))
        elif self.mode == 'give':
            self.add_item(CrystalRecipientSelect(
                row=2, placeholder='選擇接收結晶的冒險者', min_values=1, max_values=1))

        apply_label, disabled, style = None, True, discord.ButtonStyle.success
        if self.mode == 'socket':
            disabled = not self.selected_crystal or not self.selected_equipment
            apply_label = '確認鑲嵌'
            crystal = self.crystals.get(self.selected_crystal)
            entry = self.equipment.get(self.selected_equipment)
            if crystal and entry and any(
                    other.equipment_instance_id == entry.instance_id
                    and other.crystal_type == crystal.crystal_type
                    for other in all_crystals):
                apply_label, style = '覆蓋並摧毀舊結晶', discord.ButtonStyle.danger
        elif self.mode == 'remove':
            apply_label = f'支付 {CRYSTAL_REMOVAL_PRICE:,} 金幣拆除'
            disabled = not self.selected_equipment or not self.selected_slot
        elif self.mode == 'sell':
            crystal = self.crystals.get(self.selected_crystal)
            apply_label = (f'出售（{QUALITY_SELL_PRICES[crystal.quality]:,} 金幣）'
                           if crystal else '出售')
            disabled = crystal is None
        elif self.mode == 'give':
            apply_label = '確認給予'
            disabled = not self.selected_crystal or self.recipient is None
        if apply_label:
            self._button(apply_label, 'apply', 3, disabled=disabled, style=style)
        self._button('上一頁', 'previous', 3, disabled=self.page == 0)
        self._button('下一頁', 'next', 3, disabled=self.page == pages - 1)
        self._button('返回裁縫所', 'tailor', 4)
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)

    def embed(self, notice=None):
        descriptions = {
            'inventory': '查看持有結晶、隨機詞條與鑲嵌狀態。',
            'socket': '將未鑲嵌結晶免費裝入相同類型的菁英裝備槽；覆蓋會摧毀舊結晶。',
            'remove': f'支付 {CRYSTAL_REMOVAL_PRICE:,} 金幣，完整拆下已鑲嵌的結晶。',
            'sell': '將未鑲嵌的顏料結晶出售給漢娜。',
            'give': '將未鑲嵌的顏料結晶交給另一位冒險者。',
        }
        embed = discord.Embed(title=f'漢娜的裁縫所｜{MODE_LABELS[self.mode]}',
                              description=descriptions[self.mode], color=0xA855F7)
        crystal = self.crystals.get(self.selected_crystal)
        if crystal:
            location = (f'已鑲嵌於裝備 #{crystal.equipment_instance_id}'
                        if crystal.equipment_instance_id else '未鑲嵌')
            job = f'\n限定職業：{crystal.job}' if crystal.job else ''
            embed.add_field(name=f'#{crystal.instance_id} {crystal.name}', value=(
                f'{crystal_affix_name(crystal)}：{crystal_effect_text(crystal)}\n'
                f'狀態：{location}{job}\n來源：第 {crystal.source_stage} 幕'), inline=False)
        entry = self.equipment.get(self.selected_equipment)
        if entry:
            embed.add_field(name=f'{entry.item.name} #{entry.instance_id}',
                            value=item_text(entry.item), inline=False)
        if self.recipient is not None:
            embed.add_field(name='接收者', value=getattr(self.recipient, 'mention',
                                                        f'<@{self.recipient.id}>'))
        embed.add_field(name='持有資源', value=(
            f'顏料結晶：{len(self.crystals)} 顆｜'
            f'金幣：{self.cog.store.gold(self.guild_id, self.owner.id):,}'), inline=False)
        if notice:
            embed.add_field(name='加工結果', value=notice, inline=False)
        embed.set_footer(text=f'第 {self.page + 1}/{self.pages} 頁｜所有結晶加工均在漢娜的裁縫所進行。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請從自己的漢娜裁縫所面板操作。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('裁縫所面板已關閉，請從 /冒險 重新進入。', ephemeral=True)
                return
            if action == 'tailor':
                await navigate(self, interaction, 'tailor')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='你離開了漢娜的裁縫所。', embed=None, view=None)
                return
            notice = None
            try:
                if action.startswith('mode:') and action.split(':', 1)[1] in MODE_LABELS:
                    self.mode, self.page = action.split(':', 1)[1], 0
                    self.selected_crystal = self.selected_equipment = self.selected_slot = None
                    self.recipient = None
                elif action == 'crystal':
                    crystal_id = int(value)
                    if crystal_id not in self.crystals:
                        raise CharacterError('這顆結晶已不存在，請重新整理。')
                    self.selected_crystal = crystal_id
                    self.selected_equipment = None
                elif action == 'equipment':
                    if value not in self.equipment:
                        raise CharacterError('這件裝備已不存在，請重新整理。')
                    self.selected_equipment, self.selected_slot = value, None
                elif action == 'slot':
                    if value not in CRYSTAL_TYPES:
                        raise CharacterError('無效的結晶槽位。')
                    self.selected_slot = value
                elif action == 'recipient':
                    self.recipient = value
                elif action in ('previous', 'next'):
                    self.page = max(0, self.page + (1 if action == 'next' else -1))
                    self.selected_crystal = self.selected_equipment = self.selected_slot = None
                    self.recipient = None
                elif action == 'apply':
                    store = self.cog.painted_maze.crystals
                    if self.mode == 'socket':
                        crystal = store.socket(
                            self.guild_id, self.owner.id, self.selected_crystal,
                            self.selected_equipment, replace_existing=True)
                        notice = f'已鑲嵌【{crystal.name}・{crystal_affix_name(crystal)}】。'
                    elif self.mode == 'remove':
                        crystal = store.remove(
                            self.guild_id, self.owner.id, self.selected_equipment,
                            self.selected_slot)
                        notice = f'已支付 {CRYSTAL_REMOVAL_PRICE:,} 金幣拆除【{crystal.name}】。'
                    elif self.mode == 'sell':
                        price = store.sell(self.guild_id, self.owner.id, self.selected_crystal)
                        notice = f'已出售結晶，獲得 {price:,} 金幣。'
                    elif self.mode == 'give':
                        if (self.recipient.bot or self.recipient.id == self.owner.id
                                or not self.cog.store.has_player(self.guild_id, self.recipient.id)):
                            raise CharacterError('對方不是可收取結晶的其他冒險者。')
                        crystal = store.transfer(
                            self.guild_id, self.owner.id, self.selected_crystal,
                            self.recipient.id)
                        notice = f'已將【{crystal.name}・{crystal_affix_name(crystal)}】給予 {self.recipient.mention}。'
                    else:
                        raise CharacterError('目前頁面沒有可確認的加工操作。')
                    self.selected_crystal = self.selected_equipment = self.selected_slot = None
                    self.recipient = None
            except (CharacterError, TypeError, ValueError) as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(
                embed=self.embed(notice), view=self,
                allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(
                    content='裁縫所面板已逾時，請從 /冒險 重新進入。', view=None)
            except discord.HTTPException:
                pass
