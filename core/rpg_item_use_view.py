"""Extensible private panel for inventory recipes and usable key items."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_menu import add_back, navigate


ITEM_ACTIONS = {
    'recipe:paint_set': ('組合噴漆罐套組', '消耗紅、黃、藍色噴漆罐各 1 個'),
    'paint:set': ('使用噴漆罐套組', '召喚特殊四階討伐「城崎諾亞」'),
    'noah:unfinished': ('使用未完成的魔女畫作', '總力戰用途尚未開放；目前不會消耗'),
}


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
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner.id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message('這是其他冒險者的道具面板，請從自己的背包開啟。', ephemeral=True)
            return False
        return True

    def rebuild(self):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        self.catalog = ['recipe:paint_set']
        self.catalog.extend(key for key in ('paint:set', 'noah:unfinished') if counts.get(key, 0) > 0)
        if self.selected not in self.catalog:
            self.selected = self.catalog[0]
        self.clear_items()
        self.add_item(ActionSelect(row=0, placeholder='選擇要進行的道具操作', options=[
            discord.SelectOption(label=ITEM_ACTIONS[key][0], value=key,
                                 description=ITEM_ACTIONS[key][1], default=key == self.selected)
            for key in self.catalog]))
        for label, action, style in (
            ('確認使用', 'use', discord.ButtonStyle.success),
            ('重新整理', 'refresh', discord.ButtonStyle.secondary),
            ('關閉', 'close', discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, row=1, style=style)
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
        paints = '、'.join(f'{ITEMS[key].name} ×{counts.get(key, 0)}'
                          for key in ('paint:red', 'paint:yellow', 'paint:blue'))
        embed = discord.Embed(
            title='安安大冒險｜使用道具',
            description=(f'目前選擇：**{selected_name}**\n{selected_description}\n\n'
                         f'三色存量：{paints}\n'
                         f'噴漆罐套組 ×{counts.get("paint:set", 0)}｜'
                         f'未完成的魔女畫作 ×{counts.get("noah:unfinished", 0)}'),
            color=0xD65A88)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='只有確認成功才會消耗材料或道具；尚未開放的用途不會消耗物品。')
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

            try:
                if self.selected == 'recipe:paint_set':
                    item = self.cog.characters.combine_paint_set(self.guild_id, self.owner.id)
                    notice = f'已消耗三色噴漆罐各 1，組合成 {item.name}。'
                elif self.selected == 'noah:unfinished':
                    raise CharacterError('未完成的魔女畫作將用於之後的總力戰，目前尚未開放，也沒有消耗。')
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
