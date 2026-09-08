"""Private five-ingredient cooking panel; completed meals open in the tavern."""
import asyncio
from collections import Counter

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate
from core.rpg_provisions import INGREDIENT_COUNT, INGREDIENTS, effect_text


class ProvisionView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.ingredients = []
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                '請從自己的冒險面板進入酒館料理。', ephemeral=True)
            return False
        return True

    def _button(self, label, action, row, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)

        async def callback(interaction):
            await self.handle(interaction, action)

        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        selected = Counter(self.ingredients)
        available = [key for key in INGREDIENTS if counts.get(key, 0) > selected[key]]
        self.clear_items()
        options = [discord.SelectOption(
            label=ITEMS[key].name, value=key,
            description=(f'持有 {counts.get(key, 0) - selected[key]}｜'
                         f'{INGREDIENTS[key].tag}・品質 {INGREDIENTS[key].quality}'
                         + (f'・餘韻 +{INGREDIENTS[key].aftertaste}'
                            if INGREDIENTS[key].aftertaste else ''))[:100])
            for key in available]
        self.add_item(PanelSelect(
            'ingredient', row=0, placeholder='選擇下一份食材' if options else '沒有可加入的料理素材',
            disabled=not options or len(self.ingredients) >= INGREDIENT_COUNT,
            options=options or [discord.SelectOption(label='沒有可用食材', value='empty')]))
        self._button('移除最後一份', 'remove', 1, not self.ingredients)
        self._button('重新選擇', 'reset', 1, not self.ingredients)
        self._button('完成料理並開桌', 'cook', 1, len(self.ingredients) != INGREDIENT_COUNT,
                     discord.ButtonStyle.success)
        self._button('返回酒館', 'tavern', 2)
        self._button('關閉', 'close', 2)

    def embed(self, notice=None):
        state = self.cog.provisions.state(self.guild_id, self.owner.id)
        selected = Counter(self.ingredients)
        lines = [f'{ITEMS[key].name} ×{amount}｜{INGREDIENTS[key].tag}・品質 {INGREDIENTS[key].quality}'
                 + (f'・餘韻 +{INGREDIENTS[key].aftertaste}' if INGREDIENTS[key].aftertaste else '')
                 for key, amount in selected.items()]
        selection = '\n'.join(lines) or '尚未選擇食材。'
        selection += f'\n\n已選擇 {len(self.ingredients)}/{INGREDIENT_COUNT} 份'
        embed = discord.Embed(
            title='安安大冒險｜酒館料理', color=0xF59E0B,
            description=('選擇五份魚、作物、水草或藥草完成料理。食材可以重複；'
                         '品質、搭配與多樣性決定評分，完成後會在酒館專用頻道公開開桌。'))
        embed.add_field(name='料理技能', value=(
            f'Lv.{state["level"]}｜累積 {state["xp"]:,} XP'
            + (f'｜距離下一級 {state["next_xp"] - state["level_xp"]:,} XP'
               if state['next_xp'] is not None else '｜已達最高等級')), inline=False)
        embed.add_field(name='已選食材', value=selection, inline=False)
        if len(self.ingredients) == INGREDIENT_COUNT:
            try:
                data = self.cog.provisions.preview(self.ingredients, self.guild_id, self.owner.id)
                tags = '、'.join(f'{tag} {amount}' for tag, amount in data['tag_counts'].items())
                secondary = f'／副效果：{data["secondary_tag"]}' if data['secondary_tag'] else ''
                embed.add_field(name='料理預覽', value=(
                    f'**{data["grade"]} 級｜{data["name"]}**\n'
                    f'美味度 {data["score"]}｜品質 {data["quality"]}｜多樣性 {data["unique"]}｜搭配 {data["pairings"]}\n'
                    f'主效果：{data["primary_tag"]}{secondary}\n{effect_text(data["effect"])}\n'
                    f'標籤：{tags}\n總效果份數 {data["total_portions"]}｜可入席 {data["capacity"]} 人｜每人持續 {data["duration"]} 場\n'
                    f'完成取得 {data["cooking_xp"]:,} 料理 XP'), inline=False)
            except CharacterError as exc:
                embed.add_field(name='料理預覽', value=str(exc), inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice[:1024], inline=False)
        embed.set_footer(text='料理公開領取 30 分鐘；取得的效果保留 24 小時，每次正式開戰消耗一場。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('料理面板已關閉，請重新進入酒館。', ephemeral=True)
                return
            if action == 'tavern':
                await navigate(self, interaction, 'tavern')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='料理面板已關閉。', embed=None, view=None)
                return
            notice = None
            try:
                if action == 'ingredient' and value in INGREDIENTS:
                    if len(self.ingredients) >= INGREDIENT_COUNT:
                        raise CharacterError('已經選滿五份食材。')
                    counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
                    if counts.get(value, 0) <= self.ingredients.count(value):
                        raise CharacterError('背包中沒有更多這項食材。')
                    self.ingredients.append(value)
                elif action == 'remove':
                    if self.ingredients:
                        self.ingredients.pop()
                elif action == 'reset':
                    self.ingredients.clear()
                elif action == 'cook':
                    if len(self.ingredients) != INGREDIENT_COUNT:
                        raise CharacterError('請先選滿五份食材。')
                    await interaction.response.defer()
                    try:
                        message, meal = await self.cog.tavern.serve_meal(interaction, self.ingredients)
                    except CharacterError as exc:
                        self.rebuild()
                        await self.origin.edit_original_response(embed=self.embed(str(exc)), view=self)
                        return
                    except discord.HTTPException:
                        self.rebuild()
                        await self.origin.edit_original_response(
                            embed=self.embed('料理公告發布失敗，食材與料理 XP 已退還。'), view=self)
                        return
                    data = meal['data']
                    self.ingredients.clear()
                    self.rebuild()
                    await self.origin.edit_original_response(
                        embed=self.embed(f'完成 {data["name"]}，取得 {data["cooking_xp"]:,} 料理 XP：{message.jump_url}'),
                        view=self, allowed_mentions=discord.AllowedMentions.none())
                    return
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
                await self.origin.edit_original_response(content='料理面板已逾時，請重新進入酒館。', view=None)
            except discord.HTTPException:
                pass
