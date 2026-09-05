"""Cooking/alchemy crafting and a separate equipment-side loadout panel."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate
from core.rpg_provisions import FOODS, POTIONS, RECIPES


GROUPS = {
    'food': ('料理', list(FOODS)),
    'potion1': ('初級藥水', [key for key in POTIONS if key.startswith('potion:1:')]),
    'potion2': ('中級藥水', [key for key in POTIONS if key.startswith('potion:2:')]),
}


class ProvisionView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.group, self.recipe_id = 'food', next(iter(FOODS))
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 生活 → 料理／煉金 開啟自己的面板。', ephemeral=True)
            return False
        return True

    def _button(self, label, action, row, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        recipes = GROUPS[self.group][1]
        if self.recipe_id not in recipes:
            self.recipe_id = recipes[0]
        self.clear_items()
        self.add_item(PanelSelect('recipe_group', row=0, placeholder='選擇製作分類', options=[
            discord.SelectOption(label=label, value=key, default=key == self.group)
            for key, (label, _) in GROUPS.items()]))
        self.add_item(PanelSelect('recipe', row=1, placeholder='選擇配方', options=[
            discord.SelectOption(label=ITEMS[key].name, value=key,
                                 description=' + '.join(ITEMS[item].name for item in RECIPES[key]['ingredients']),
                                 default=key == self.recipe_id) for key in recipes]))
        maximum = self.cog.provisions.max_craftable(self.guild_id, self.owner.id, self.recipe_id)
        self._button('製作 1 個', 'craft:1', 2, maximum < 1, discord.ButtonStyle.success)
        self._button('製作 5 個', 'craft:5', 2, maximum < 5, discord.ButtonStyle.success)
        self._button(f'製作全部（{maximum}）', 'craft:all', 2, maximum < 1,
                     discord.ButtonStyle.success)
        self._button('返回生活', 'life', 3)
        self._button('重新整理', 'refresh', 3)
        self._button('關閉', 'close', 3)

    def embed(self, notice=None):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        item = ITEMS[self.recipe_id]
        recipe = RECIPES[self.recipe_id]
        ingredient_text = '\n'.join(
            f'{ITEMS[key].name}：{counts.get(key, 0)}/1' for key in recipe['ingredients'])
        maximum = self.cog.provisions.max_craftable(self.guild_id, self.owner.id, self.recipe_id)
        embed = discord.Embed(title='安安大冒險｜料理／煉金', color=0xF59E0B,
                              description='使用釣魚與農耕素材製作料理及藥水；攜帶設定請前往裝備／能力的「討伐補給」。')
        embed.add_field(name=f'製作：{item.name}',
                        value=f'{item.description}\n\n{ingredient_text}\n最多可製作：{maximum} 個', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice[:1024], inline=False)
        embed.set_footer(text='料理於存活且 HP 首次降至 40% 以下時自動食用；藥水效果持續整場討伐。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('料理／煉金面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'life':
                await navigate(self, interaction, 'life')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='料理／煉金面板已關閉。', embed=None, view=None)
                return
            notice = None
            try:
                if action == 'recipe_group' and value in GROUPS:
                    self.group = value
                    self.recipe_id = GROUPS[value][1][0]
                elif action == 'recipe' and value in RECIPES:
                    self.recipe_id = value
                elif action == 'craft' or action.startswith('craft:'):
                    requested = action.partition(':')[2]
                    quantity = (self.cog.provisions.max_craftable(
                        self.guild_id, self.owner.id, self.recipe_id)
                        if requested == 'all' else int(requested or '1'))
                    if quantity < 1:
                        raise CharacterError('目前沒有足夠材料可以製作。')
                    item = self.cog.provisions.craft(
                        self.guild_id, self.owner.id, self.recipe_id, quantity)
                    notice = f'成功製作 {item.name} ×{quantity}。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='料理／煉金面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass


class ProvisionLoadoutView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 裝備／能力 → 討伐補給 開啟自己的面板。', ephemeral=True)
            return False
        return True

    def _button(self, label, action, row):
        button = discord.ui.Button(label=label, row=row)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        loadout = self.cog.provisions.loadout(self.guild_id, self.owner.id)
        self.clear_items()
        for row, (kind, catalog, placeholder) in enumerate((
                ('food', FOODS, '選擇討伐料理'), ('potion', POTIONS, '選擇討伐藥水'))):
            options = [discord.SelectOption(label='不攜帶', value=f'none:{kind}',
                                            default=kind not in loadout)]
            options.extend(discord.SelectOption(label=ITEMS[key].name, value=key,
                                                description=f'持有 {counts[key]}｜{ITEMS[key].description}'[:100],
                                                default=loadout.get(kind) == key)
                           for key in catalog if counts.get(key, 0))
            self.add_item(PanelSelect(f'loadout_{kind}', row=row, placeholder=placeholder, options=options))
        self._button('返回裝備', 'equipment', 2)
        self._button('重新整理', 'refresh', 2)
        self._button('關閉', 'close', 2)

    def embed(self, notice=None):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        loadout = self.cog.provisions.loadout(self.guild_id, self.owner.id)
        food = loadout.get('food')
        potion = loadout.get('potion')
        def line(key):
            return f'**{ITEMS[key].name}** ×{counts.get(key, 0)}\n{ITEMS[key].description}' if key else '不攜帶'
        embed = discord.Embed(title='安安大冒險｜討伐補給', color=0xF59E0B,
                              description='料理與藥水各可攜帶一份；報名截止並開戰時各消耗一份，效果固定於該場討伐。')
        embed.add_field(name='料理', value=line(food), inline=False)
        embed.add_field(name='藥水', value=line(potion), inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice[:1024], inline=False)
        embed.set_footer(text='料理於 HP 首次降至 40% 以下時自動食用；藥水開戰時生效並持續整場。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('討伐補給面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'equipment':
                await navigate(self, interaction, 'equipment')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='討伐補給面板已關閉。', embed=None, view=None)
                return
            notice = None
            try:
                if action in ('loadout_food', 'loadout_potion'):
                    kind = action.removeprefix('loadout_')
                    item_id = None if value == f'none:{kind}' else value
                    item = self.cog.provisions.select(self.guild_id, self.owner.id, kind, item_id)
                    notice = f'討伐時將攜帶 {item.name}。' if item else f'已取消攜帶{"料理" if kind == "food" else "藥水"}。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='討伐補給面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
