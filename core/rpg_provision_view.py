"""Five-ingredient cooking panel; completed meals open in the tavern."""
import asyncio
from collections import Counter

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate
from core.rpg_provisions import (COOKING_PRESET_SLOTS, INGREDIENT_COUNT, INGREDIENTS,
                                 effect_text, guest_reward_target)


class RenameRecipePresetModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='重新命名料理配方')
        self.panel = panel
        self.name = discord.ui.TextInput(
            label='配方名稱', default=panel.current_preset()['name'],
            min_length=1, max_length=20)
        self.add_item(self.name)

    async def on_submit(self, interaction):
        await self.panel.handle(interaction, 'preset_rename_value', self.name.value)


class ProvisionView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.ingredients = []
        self.ingredient_page = 0
        self.preset_slot = 1
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

    def current_preset(self):
        return self.cog.provisions.preset(
            self.guild_id, self.owner.id, self.preset_slot)

    @staticmethod
    def _recipe_text(ingredients):
        if not ingredients:
            return '尚未保存'
        return '、'.join(f'{ITEMS[key].name} ×{amount}'
                        for key, amount in Counter(ingredients).items())

    def rebuild(self):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        selected = Counter(self.ingredients)
        has_seasoning = any(INGREDIENTS[key].seasoning for key in self.ingredients)
        available = [key for key in INGREDIENTS
                     if counts.get(key, 0) > selected[key]
                     and not (has_seasoning and INGREDIENTS[key].seasoning)]
        page_count = max(1, (len(available) + 24) // 25)
        self.ingredient_page = min(self.ingredient_page, page_count - 1)
        shown = available[self.ingredient_page * 25:(self.ingredient_page + 1) * 25]
        self.clear_items()
        options = [discord.SelectOption(
            label=ITEMS[key].name, value=key,
            description=(f'持有 {counts.get(key, 0) - selected[key]}｜'
                         + ('調味料' if INGREDIENTS[key].seasoning else
                            f'{INGREDIENTS[key].tag_text}・品質 {INGREDIENTS[key].quality}')
                         + (f'・餘韻 +{INGREDIENTS[key].aftertaste}'
                            if INGREDIENTS[key].aftertaste else ''))[:100])
            for key in shown]
        self.add_item(PanelSelect(
            'ingredient', row=0, placeholder='選擇下一份食材' if options else '沒有可加入的料理素材',
            disabled=not options or len(self.ingredients) >= INGREDIENT_COUNT,
            options=options or [discord.SelectOption(label='沒有可用食材', value='empty')]))
        self._button('移除最後一份', 'remove', 1, not self.ingredients)
        self._button('重新選擇', 'reset', 1, not self.ingredients)
        self._button('完成料理並開桌', 'cook', 1,
                     len(self.ingredients) != INGREDIENT_COUNT,
                     discord.ButtonStyle.success)
        self._button('捐給監獄', 'donate', 1, not self.ingredients,
                     discord.ButtonStyle.primary)
        last_recipe = self.cog.provisions.last_recipe(self.guild_id, self.owner.id)
        can_repeat = bool(last_recipe) and all(
            counts.get(key, 0) >= amount for key, amount in Counter(last_recipe).items())
        self._button('載入上一份配方', 'repeat', 1, not can_repeat,
                     discord.ButtonStyle.primary)
        self._button('返回酒館', 'tavern', 2)
        self._button('上一頁食材', 'ingredient_prev', 2, self.ingredient_page == 0)
        self._button('下一頁食材', 'ingredient_next', 2, self.ingredient_page + 1 >= page_count)
        self._button('關閉', 'close', 2)
        presets = self.cog.provisions.presets(self.guild_id, self.owner.id)
        self.add_item(PanelSelect(
            'preset_slot', row=3, placeholder='選擇料理配方', options=[
                discord.SelectOption(
                    label=preset['name'], value=str(preset['slot']),
                    description=self._recipe_text(preset['ingredients'])[:100],
                    default=preset['slot'] == self.preset_slot)
                for preset in presets]))
        preset = self.current_preset()
        can_load_preset = bool(preset['ingredients']) and all(
            counts.get(key, 0) >= amount
            for key, amount in Counter(preset['ingredients'] or ()).items())
        self._button('保存目前配方', 'preset_save', 4,
                     len(self.ingredients) != INGREDIENT_COUNT,
                     discord.ButtonStyle.success)
        self._button('載入配方', 'preset_load', 4, not can_load_preset,
                     discord.ButtonStyle.primary)
        self._button('重新命名', 'preset_rename', 4)
        self._button('清空配方', 'preset_clear', 4, not preset['ingredients'],
                     discord.ButtonStyle.danger)

    def embed(self, notice=None):
        state = self.cog.provisions.state(self.guild_id, self.owner.id)
        selected = Counter(self.ingredients)
        lines = [f'{ITEMS[key].name} ×{amount}｜'
                 + ('調味料' if INGREDIENTS[key].seasoning else
                    f'{INGREDIENTS[key].tag_text}・品質 {INGREDIENTS[key].quality}')
                 + (f'・餘韻 +{INGREDIENTS[key].aftertaste}' if INGREDIENTS[key].aftertaste else '')
                 for key, amount in selected.items()]
        selection = '\n'.join(lines) or '尚未選擇食材。'
        selection += f'\n\n已選擇 {len(self.ingredients)}/{INGREDIENT_COUNT} 份'
        embed = discord.Embed(
            title='安安大冒險｜酒館料理', color=0xF59E0B,
            description=('選擇五份魚、作物、水草、藥草、肉類或調味料完成料理。食材可以重複；'
                         '每桌最多一份調味料，品質、搭配與多樣性決定評分。'))
        embed.add_field(name='料理技能', value=(
            f'Lv.{state["level"]}｜累積 {state["xp"]:,} XP'
            + (f'｜距離下一級 {state["next_xp"] - state["level_xp"]:,} XP'
               if state['next_xp'] is not None else '｜已達最高等級')), inline=False)
        embed.add_field(name='已選食材', value=selection, inline=False)
        preset = self.current_preset()
        embed.add_field(name=f'目前配方格｜{preset["name"]}',
                        value=self._recipe_text(preset['ingredients']), inline=False)
        if len(self.ingredients) == INGREDIENT_COUNT:
            try:
                data = self.cog.provisions.preview(self.ingredients, self.guild_id, self.owner.id)
                tags = '、'.join(f'{tag} {amount}' for tag, amount in data['tag_counts'].items())
                reward_guests = guest_reward_target(data['capacity'])
                xp_text = (f'潛在料理 XP {data["cooking_xp"]:,}｜完成先取得 '
                           f'{data["initial_cooking_xp"]:,}，前 {reward_guests} 位不同客人'
                           '享用後共同解鎖其餘 75%')
                grade_text = data['grade']
                if data['potential_grade'] != data['grade']:
                    required_level = {'SS': 40, 'SSS': 80}[data['potential_grade']]
                    grade_text += (f'（潛在 {data["potential_grade"]}／目前上限 {data["grade_cap"]}／'
                                   f'Lv.{required_level} 解鎖）')
                secondary = (f'／副效果：{data["secondary_tag"]} '
                             f'({data["secondary_percent"]}%)' if data['secondary_tag'] else '')
                embed.add_field(name='料理預覽', value=(
                    f'**{grade_text} 級｜{data["name"]}**\n'
                    f'美味度 {data["score"]}｜品質 {data["quality"]}｜多樣性 {data["unique"]}｜搭配 {data["pairings"]}'
                    f'｜調味 +{data["seasoning_bonus"]}\n'
                    f'主效果：{data["primary_tag"]}{secondary}\n{effect_text(data["effect"])}\n'
                    f'標籤：{tags}\n總效果份數 {data["total_portions"]}｜可入席 {data["capacity"]} 人｜每人持續 {data["duration"]} 場\n'
                    f'{xp_text}'),
                    inline=False)
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
            if action == 'preset_rename':
                await interaction.response.send_modal(RenameRecipePresetModal(self))
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
                elif action == 'ingredient_prev':
                    self.ingredient_page = max(0, self.ingredient_page - 1)
                elif action == 'ingredient_next':
                    self.ingredient_page += 1
                elif action == 'preset_slot':
                    if (not isinstance(value, str) or not value.isdigit()
                            or not 1 <= int(value) <= COOKING_PRESET_SLOTS):
                        raise CharacterError('無效的料理配方格。')
                    self.preset_slot = int(value)
                elif action == 'preset_save':
                    preset = self.cog.provisions.save_preset(
                        self.guild_id, self.owner.id, self.preset_slot, self.ingredients)
                    notice = f'已將目前選擇保存至「{preset["name"]}」。'
                elif action == 'preset_load':
                    preset = self.current_preset()
                    if not preset['ingredients']:
                        raise CharacterError('這個料理配方格尚未保存內容。')
                    counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
                    missing = [f'{ITEMS[key].name} {counts.get(key, 0)}/{amount}'
                               for key, amount in Counter(preset['ingredients']).items()
                               if counts.get(key, 0) < amount]
                    if missing:
                        raise CharacterError('素材不足：' + '、'.join(missing))
                    self.ingredients = list(preset['ingredients'])
                    notice = f'已載入「{preset["name"]}」，尚未消耗素材。'
                elif action == 'preset_rename_value':
                    preset = self.cog.provisions.rename_preset(
                        self.guild_id, self.owner.id, self.preset_slot, value)
                    notice = f'配方已重新命名為「{preset["name"]}」。'
                elif action == 'preset_clear':
                    name = self.current_preset()['name']
                    self.cog.provisions.clear_preset(
                        self.guild_id, self.owner.id, self.preset_slot)
                    notice = f'已清空「{name}」。'
                elif action == 'donate':
                    result = self.cog.provisions.donate(
                        self.guild_id, self.owner.id, self.ingredients)
                    self.ingredients.clear()
                    notice = (f'已將 {result["quantity"]} 份食材捐給監獄，'
                              f'取得 {result["xp"]:,} 料理 XP。')
                elif action == 'repeat':
                    recipe = self.cog.provisions.last_recipe(self.guild_id, self.owner.id)
                    if not recipe:
                        raise CharacterError('還沒有可以載入的上一份配方。')
                    counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
                    missing = [f'{ITEMS[key].name} {counts.get(key, 0)}/{amount}'
                               for key, amount in Counter(recipe).items()
                               if counts.get(key, 0) < amount]
                    if missing:
                        raise CharacterError('素材不足：' + '、'.join(missing))
                    self.ingredients = list(recipe)
                    notice = '已載入上一份配方，可繼續調整、完成料理或捐給監獄。'
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
                    reward_guests = guest_reward_target(meal['capacity'])
                    notice = (f'完成 {data["name"]}，先取得 {data["initial_cooking_xp"]:,}／'
                              f'{data["cooking_xp"]:,} 料理 XP；前 {reward_guests} 位不同客人'
                              f'享用後共同解鎖其餘經驗：{message.jump_url}')
                    await self.origin.edit_original_response(
                        embed=self.embed(notice),
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
