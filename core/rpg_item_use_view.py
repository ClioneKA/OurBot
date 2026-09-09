"""Extensible private panel for inventory recipes and usable key items."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS, MAZE_CHOICE_BOXES
from core.rpg_menu import add_back, navigate
from core.rpg_painted_maze import ENTRY_CLOSED_NOTICE, ENTRY_ENABLED, ENTRY_ROUTES, MODE_NAME


ITEM_ACTIONS = {
    'recipe:paint_set': ('組合噴漆罐套組', '消耗紅、黃、藍色噴漆罐各 1 個'),
    'paint:set': ('使用噴漆罐套組', '召喚特殊四階討伐「城崎諾亞」'),
    'noah:unfinished': ('展開未完成的魔女畫作', '建立城崎諾亞路線的 1～8 人繪境迷廊房間'),
    'painting:balloon': ('展開《氣球》的畫作', '建立繪畫之影路線的 1～8 人繪境迷廊房間'),
}
ITEM_ACTIONS.update({box_id: (f'開啟{ITEMS[box_id].name}', '選擇本職 T60 菁英武器或套裝')
                     for box_id in MAZE_CHOICE_BOXES})


class ActionSelect(discord.ui.Select):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def callback(self, interaction):
        await self.view.handle(interaction, 'select', self.values[0])


class ItemUseView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.selected = 'recipe:paint_set'
        self.closed = False
        self.lock = asyncio.Lock()
        self.cog.painted_maze.rewards.release_pending_boxes(self.guild_id, self.owner.id)
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner.id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message('這是其他冒險者的道具面板，請從自己的背包開啟。', ephemeral=True)
            return False
        return True

    def rebuild(self):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        self.catalog = ['recipe:paint_set']
        self.catalog.extend(key for key in ('paint:set', 'noah:unfinished', 'painting:balloon')
                            if counts.get(key, 0) > 0)
        self.catalog.extend(key for key in MAZE_CHOICE_BOXES if counts.get(key, 0) > 0)
        if self.selected not in self.catalog:
            self.selected = self.catalog[0]
        self.clear_items()
        self.add_item(ActionSelect(row=0, placeholder='選擇要進行的道具操作', options=[
            discord.SelectOption(label=ITEM_ACTIONS[key][0], value=key,
                                 description=ITEM_ACTIONS[key][1], default=key == self.selected)
            for key in self.catalog]))
        row = 1
        actions = ([('領取武器', 'choose:weapon', discord.ButtonStyle.success),
                    ('領取套裝', 'choose:suit', discord.ButtonStyle.success)]
                   if self.selected in MAZE_CHOICE_BOXES else
                   [('確認使用', 'use', discord.ButtonStyle.success)])
        actions.extend((('重新整理', 'refresh', discord.ButtonStyle.secondary),
                        ('關閉', 'close', discord.ButtonStyle.secondary)))
        for label, action, style in actions:
            button = discord.ui.Button(label=label, row=row, style=style)
            if action == 'use' and self.selected in ENTRY_ROUTES and not ENTRY_ENABLED:
                button.label = '暫停開放'
                button.disabled = True
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)
        add_back(self, 2)
        back = discord.ui.Button(label='返回背包', row=2)
        async def back_callback(interaction):
            await self.handle(interaction, 'backpack')
        back.callback = back_callback
        self.add_item(back)
        return counts

    def embed(self, notice=None):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        selected_name, selected_description = ITEM_ACTIONS[self.selected]
        if self.selected in ENTRY_ROUTES and not ENTRY_ENABLED:
            selected_description = ENTRY_CLOSED_NOTICE
        paints = '、'.join(f'{ITEMS[key].name} ×{counts.get(key, 0)}'
                          for key in ('paint:red', 'paint:yellow', 'paint:blue'))
        embed = discord.Embed(
            title='安安大冒險｜使用道具',
            description=(f'目前選擇：**{selected_name}**\n{selected_description}\n\n'
                         f'三色存量：{paints}\n'
                         f'噴漆罐套組 ×{counts.get("paint:set", 0)}｜'
                         f'未完成的魔女畫作 ×{counts.get("noah:unfinished", 0)}｜'
                         f'《氣球》的畫作 ×{counts.get("painting:balloon", 0)}'),
            color=0xD65A88)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text=(
            '按下武器或套裝即會消耗自選箱，選擇後不能更換。'
            if self.selected in MAZE_CHOICE_BOXES else
            '入場畫作會在房主按下「開始探索」後才消耗；只建立房間不會消耗。'))
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('道具面板已關閉，請從背包重新開啟。', ephemeral=True)
                return
            if action in ('home', 'backpack'):
                await navigate(self, interaction, 'home' if action == 'home' else 'backpack')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='道具面板已關閉。', embed=None, view=None)
                return

            self.rebuild()
            if action == 'select':
                if value not in self.catalog:
                    await interaction.response.edit_message(embed=self.embed('這項道具操作目前不可用。'), view=self)
                    return
                self.selected = value
                self.rebuild()
                await interaction.response.edit_message(embed=self.embed(), view=self)
                return
            if action != 'use':
                if action.startswith('choose:') and self.selected in MAZE_CHOICE_BOXES:
                    try:
                        item_id, _equipment_id = self.cog.painted_maze.rewards.open_choice_box(
                            self.guild_id, self.owner.id, self.selected,
                            action.split(':', 1)[1])
                        notice = f'已開啟自選箱，獲得【{ITEMS[item_id].name}】。'
                    except CharacterError as exc:
                        notice = str(exc)
                    self.rebuild()
                    await interaction.response.edit_message(embed=self.embed(notice), view=self)
                    return
                await interaction.response.edit_message(embed=self.embed(), view=self)
                return

            if self.selected == 'paint:set':
                await interaction.response.defer()
                try:
                    channel, _, raid = await self.cog.raids.summon_noah(interaction.guild, self.owner)
                    color = {'red': '紅色', 'yellow': '黃色', 'blue': '藍色'}[raid['monster']['primary_color']]
                    notice = f'已消耗噴漆罐套組，在 {channel.mention} 召喚四階城崎諾亞；起始顏料為{color}，你已自動報名。'
                except CharacterError as exc:
                    notice = str(exc)
                except discord.HTTPException:
                    notice = '特殊討伐發布失敗，噴漆罐套組已退回背包，請稍後再試。'
                self.rebuild()
                await interaction.edit_original_response(embed=self.embed(notice), view=self)
                return

            if self.selected in ('noah:unfinished', 'painting:balloon'):
                await interaction.response.defer()
                try:
                    room = await self.cog.painted_maze.create(interaction, self.selected)
                    notice = (f'已建立 **{MODE_NAME} #{room["number"]}** 與私人討論串；'
                              '畫作將在開始探索時消耗。')
                except (CharacterError, discord.HTTPException) as exc:
                    notice = str(exc)
                self.rebuild()
                await interaction.edit_original_response(embed=self.embed(notice), view=self)
                return

            try:
                if self.selected == 'recipe:paint_set':
                    item = self.cog.characters.combine_paint_set(self.guild_id, self.owner.id)
                    notice = f'已消耗三色噴漆罐各 1，組合成 {item.name}。'
                else:
                    raise CharacterError('這項道具操作目前不可用。')
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
                await self.origin.edit_original_response(content='道具面板已逾時，請從背包重新開啟。', view=None)
            except discord.HTTPException:
                pass
