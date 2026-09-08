"""Pigment-crystal services inside Tono Hanna's tailor shop."""
import asyncio

import discord

from core.rpg_character import CharacterError, item_text
from core.rpg_crystals import (CRYSTAL_TYPES,
                               QUALITY_SELL_PRICES, crystal_affix_name,
                               crystal_effect_text)
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate


PAGE_SIZE = 20
MODE_LABELS = {
    'inventory': '結晶一覽',
    'socket': '裝備鑲嵌／替換',
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
        self.mode, self.page = 'socket', 0
        self.equipment_page = 0
        self.selected_crystal = self.selected_equipment = None
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
        return f'{crystal_effect_text(crystal)}｜{state}{job}'[:100]

    def _equipment_entries(self):
        return [entry for entry in self.cog.characters.inventory_entries(
            self.guild_id, self.owner.id)
            if entry.instance_id and entry.item.crystal_slots]

    def _compatible(self, crystal, entry):
        return (entry is not None and crystal.equipment_instance_id is None
                and crystal.crystal_type in entry.item.crystal_slots
                and (crystal.crystal_type != 'source' or crystal.job == entry.item.job))

    def _old_crystal(self, crystals, entry, crystal):
        return next((other for other in crystals
                     if entry and crystal and other.equipment_instance_id == entry.instance_id
                     and other.crystal_type == crystal.crystal_type), None)

    def rebuild(self):
        all_crystals = self.cog.painted_maze.crystals.inventory(self.guild_id, self.owner.id)
        equipment = self._equipment_entries()
        self.crystals = {crystal.instance_id: crystal for crystal in all_crystals}
        self.equipment = {entry.reference: entry for entry in equipment}
        if self.selected_equipment not in self.equipment:
            self.selected_equipment = None
        entry = self.equipment.get(self.selected_equipment)
        if self.mode == 'socket':
            primary = [crystal for crystal in all_crystals if self._compatible(crystal, entry)]
        elif self.mode in ('sell', 'give'):
            primary = [crystal for crystal in all_crystals if crystal.equipment_instance_id is None]
        else:
            primary = all_crystals
        self.available_crystals = {crystal.instance_id for crystal in primary}
        if self.selected_crystal not in self.available_crystals:
            self.selected_crystal = None
        crystal = self.crystals.get(self.selected_crystal)
        self.pages = max(1, (len(primary) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, self.pages - 1)
        visible = primary[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        self.equipment_pages = max(1, (len(equipment) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.equipment_page = min(self.equipment_page, self.equipment_pages - 1)
        self.clear_items()
        for mode, label in MODE_LABELS.items():
            self._button(label, f'mode:{mode}', 0,
                         style=(discord.ButtonStyle.primary if mode == self.mode
                                else discord.ButtonStyle.secondary))
        if self.mode == 'socket':
            equipment_options = [discord.SelectOption(
                label=f'{item.item.name} #{item.instance_id}', value=item.reference,
                description=item_text(item.item)[:100],
                default=item.reference == self.selected_equipment)
                for item in equipment[self.equipment_page * PAGE_SIZE:(self.equipment_page + 1) * PAGE_SIZE]]
            self.add_item(PanelSelect(
                'equipment', row=1, placeholder='① 選擇裝備，查看目前結晶槽' if equipment_options
                else '目前沒有可鑲嵌結晶的裝備', disabled=not equipment_options,
                options=equipment_options or [discord.SelectOption(label='沒有可選裝備', value='empty')]))
        options = [discord.SelectOption(
            label=f'#{item.instance_id} {item.name}', value=str(item.instance_id),
            description=self._crystal_description(item),
            default=item.instance_id == self.selected_crystal) for item in visible]
        placeholder = '選擇顏料結晶' if options else '目前沒有可用的顏料結晶'
        if self.mode == 'socket':
            placeholder = ('請先選擇裝備' if entry is None else
                           '② 選擇相容結晶，預覽鑲嵌或替換' if options else '沒有相容的未鑲嵌結晶')
        self.add_item(PanelSelect(
            'crystal', row=2 if self.mode == 'socket' else 1, placeholder=placeholder,
            disabled=not options,
            options=options or [discord.SelectOption(label='沒有可選結晶', value='empty')]))
        if self.mode == 'give':
            self.add_item(CrystalRecipientSelect(
                row=2, placeholder='選擇接收結晶的冒險者', min_values=1, max_values=1))
        if self.mode == 'socket':
            old = self._old_crystal(all_crystals, entry, crystal)
            self._button('確認替換（摧毀舊結晶）' if old else '確認鑲嵌', 'apply', 3,
                         disabled=crystal is None or entry is None,
                         style=discord.ButtonStyle.danger if old else discord.ButtonStyle.success)
        elif self.mode == 'sell':
            self._button(f'出售（{QUALITY_SELL_PRICES[crystal.quality]:,} 金幣）' if crystal else '出售',
                         'apply', 3, disabled=crystal is None, style=discord.ButtonStyle.success)
        elif self.mode == 'give':
            self._button('確認給予', 'apply', 3, disabled=crystal is None or self.recipient is None,
                         style=discord.ButtonStyle.success)
        self._button('結晶上一頁', 'previous', 3, disabled=self.page == 0)
        self._button('結晶下一頁', 'next', 3, disabled=self.page == self.pages - 1)
        if self.mode == 'socket' and self.equipment_pages > 1:
            self._button('裝備上一頁', 'equipment_previous', 4, disabled=self.equipment_page == 0)
            self._button('裝備下一頁', 'equipment_next', 4,
                         disabled=self.equipment_page == self.equipment_pages - 1)
        self._button('返回裁縫所', 'tailor', 4)
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)

    def embed(self, notice=None):
        descriptions = {
            'inventory': '查看持有結晶與效果；前往「裝備鑲嵌／替換」可選擇裝備加工。',
            'socket': '① 選裝備查看槽位 → ② 選相容結晶 → ③ 確認鑲嵌或替換。\n空槽免費鑲嵌；已有結晶則直接替換，舊結晶會被摧毀。',
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
        if entry and self.mode == 'socket':
            embed.add_field(name=f'{entry.item.name} #{entry.instance_id}',
                            value=item_text(entry.item), inline=False)
            if self.mode == 'socket':
                for slot in entry.item.crystal_slots:
                    old = next((item for item in self.crystals.values()
                                if item.equipment_instance_id == entry.instance_id
                                and item.crystal_type == slot), None)
                    value = (f'#{old.instance_id} {old.name}・{crystal_affix_name(old)}\n'
                             f'{crystal_effect_text(old)}' if old else '空槽，可直接鑲嵌')
                    embed.add_field(name=CRYSTAL_TYPES[slot], value=value, inline=False)
                if crystal:
                    old = self._old_crystal(self.crystals.values(), entry, crystal)
                    embed.add_field(name='替換預覽' if old else '鑲嵌預覽', value=(
                        f'目前：{crystal_effect_text(old) if old else "空槽"}\n'
                        f'換成：{crystal_effect_text(crystal)}\n'
                        + ('確認後舊結晶會被摧毀。' if old else '免費鑲嵌。')), inline=False)
        if self.recipient is not None:
            embed.add_field(name='接收者', value=getattr(self.recipient, 'mention',
                                                        f'<@{self.recipient.id}>'))
        embed.add_field(name='持有資源', value=(
            f'顏料結晶：{len(self.crystals)} 顆｜'
            f'金幣：{self.cog.store.gold(self.guild_id, self.owner.id):,}'), inline=False)
        if notice:
            embed.add_field(name='加工結果', value=notice, inline=False)
        embed.set_footer(text=(f'裝備第 {self.equipment_page + 1}/{self.equipment_pages} 頁｜' if self.mode == 'socket' else '')
                         + f'結晶第 {self.page + 1}/{self.pages} 頁')
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
                    mode = action.split(':', 1)[1]
                    if mode != self.mode:
                        self.page = 0
                    self.mode = mode
                    self.recipient = None
                elif action == 'crystal':
                    crystal_id = int(value)
                    if crystal_id not in self.available_crystals:
                        raise CharacterError('請選擇目前清單中的相容結晶。')
                    self.selected_crystal = crystal_id
                elif action == 'equipment':
                    if self.mode != 'socket' or value not in self.equipment:
                        raise CharacterError('這件裝備已不存在，請重新整理。')
                    self.selected_equipment = value
                    self.selected_crystal, self.page = None, 0
                elif action in ('equipment_previous', 'equipment_next'):
                    self.equipment_page = max(0, self.equipment_page + (1 if action == 'equipment_next' else -1))
                    self.selected_equipment = self.selected_crystal = None
                    self.page = 0
                elif action == 'recipient':
                    self.recipient = value
                elif action in ('previous', 'next'):
                    self.page = max(0, self.page + (1 if action == 'next' else -1))
                    self.selected_crystal = None
                    self.recipient = None
                elif action == 'apply':
                    store = self.cog.painted_maze.crystals
                    current = store.get(self.selected_crystal)
                    if (not current or current.guild_id != self.guild_id
                            or current.user_id != self.owner.id
                            or current.equipment_instance_id is not None):
                        raise CharacterError('結晶狀態已變更，請重新選擇操作。')
                    if self.mode == 'socket':
                        entry = self.equipment.get(self.selected_equipment)
                        if not self._compatible(current, entry):
                            raise CharacterError('請先選擇裝備與相容結晶。')
                        old = self._old_crystal(self.crystals.values(), entry, current)
                        latest = self._old_crystal(store.inventory(self.guild_id, self.owner.id), entry, current)
                        if old != latest:
                            raise CharacterError('裝備槽位已變更，請查看更新後的預覽再確認。')
                        crystal = store.socket(
                            self.guild_id, self.owner.id, self.selected_crystal,
                            self.selected_equipment, replace_existing=old is not None)
                        notice = f'已{"替換" if old else "鑲嵌"}【{crystal.name}・{crystal_affix_name(crystal)}】。'
                    elif self.mode == 'sell':
                        price = store.sell(self.guild_id, self.owner.id, self.selected_crystal)
                        notice = f'已出售結晶，獲得 {price:,} 金幣。'
                    elif self.mode == 'give':
                        if (self.recipient is None or self.recipient.bot or self.recipient.id == self.owner.id
                                or not self.cog.store.has_player(self.guild_id, self.recipient.id)):
                            raise CharacterError('對方不是可收取結晶的其他冒險者。')
                        crystal = store.transfer(
                            self.guild_id, self.owner.id, self.selected_crystal,
                            self.recipient.id)
                        notice = f'已將【{crystal.name}・{crystal_affix_name(crystal)}】給予 {self.recipient.mention}。'
                    else:
                        raise CharacterError('目前頁面沒有可確認的加工操作。')
                    self.selected_crystal = None
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
