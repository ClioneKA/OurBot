"""Private Discord panel for two concurrent farming plots."""
import asyncio
import time

import discord

from core.rpg import MAX_LEVEL
from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_farming import (LOCATION_LEVELS, LOCATIONS, PLANTS, SPECIALIZATIONS,
                              SPECIALIZATION_LEVEL, farming_progress, growth_text)
from core.rpg_menu import add_back, add_favorite_toggle, add_help, navigate


class FarmingView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        level = self.cog.farming.state(self.guild_id, self.owner.id)['level']
        self.location_id = next(key for key in reversed(LOCATIONS) if level >= LOCATION_LEVELS[key])
        self.plant_id = next(key for key, plant in reversed(PLANTS.items()) if level >= plant.level)
        self.confirming_cancel = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 生活 → 農耕 開啟自己的面板。', ephemeral=True)
            return False
        return True

    def _button(self, label, action, row, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        state = self.cog.farming.state(self.guild_id, self.owner.id)
        unlocked = [key for key, plant in PLANTS.items() if state['level'] >= plant.level]
        locations = [key for key in LOCATIONS if state['level'] >= LOCATION_LEVELS[key]]
        if self.location_id not in locations:
            self.location_id = locations[0]
        if self.plant_id not in unlocked:
            self.plant_id = unlocked[0]
        self.clear_items()
        self.add_item(PanelSelect('location', row=0, placeholder='選擇農耕地點', options=[
            discord.SelectOption(label=name, value=key, default=key == self.location_id)
            for key, name in LOCATIONS.items() if key in locations]))
        self.add_item(PanelSelect('plant_choice', row=1, placeholder='選擇植物', options=[
            discord.SelectOption(label=PLANTS[key].name, value=key,
                                 description=(f'Lv.{PLANTS[key].level}｜'
                                              f'{growth_text(PLANTS[key].seconds)}｜'
                                              f'收成 {PLANTS[key].base_yield}｜{PLANTS[key].role}'),
                                 default=key == self.plant_id) for key in unlocked]))
        session = state['sessions'].get(self.location_id)
        active = bool(session and session['status'] == 'active')
        ready = bool(active and time.time() >= session['ready_at'])
        if not active:
            self.confirming_cancel = False
        if state['level'] >= SPECIALIZATION_LEVEL:
            current_specialization = state['specializations'].get(self.location_id)
            self.add_item(PanelSelect('specialization', row=2, placeholder='選擇這塊田的專精',
                disabled=active, options=[discord.SelectOption(
                    label=name, value=key,
                    description=('額外收成 1 份，Lv.100 後有 10% 機率再 +1；額外作物不給 XP'
                                 if key == 'abundance' else
                                 '收成 XP +10%，Lv.100 後提高為 +15%'),
                    default=key == current_specialization)
                    for key, name in SPECIALIZATIONS.items()]))
        if self.confirming_cancel:
            self._button('繼續種植', 'keep', 3)
            self._button('確認中斷並放棄本次收成', 'cancel_confirm', 3,
                         style=discord.ButtonStyle.danger)
        else:
            self._button('種植', 'plant', 3, active, discord.ButtonStyle.success)
            self._button('收成', 'harvest', 3, not ready, discord.ButtonStyle.primary)
            self._button('中斷種植', 'cancel', 3, not active, discord.ButtonStyle.danger)
            self._button('關閉成熟通知' if state['notify'] else '開啟成熟通知', 'notify', 3)
        add_help(self, 4, 'gathering', 'farming')
        add_favorite_toggle(self, 4, 'farming')
        add_back(self, 4, 'life', '返回生活')
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)
        return state

    def embed(self, notice=None):
        state = self.cog.farming.state(self.guild_id, self.owner.id)
        inventory = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        level, progress, required = farming_progress(state['xp'])
        xp_text = (f'已達最高等級 Lv.{MAX_LEVEL}｜累積 {state["xp"]:,} XP' if required is None
                   else f'{progress:,}／{required:,} XP｜累積 {state["xp"]:,} XP')
        embed = discord.Embed(title='安安大冒險｜農耕', color=0x65A30D,
                              description=f'農耕 Lv.**{level}**｜{xp_text}\n已解鎖的田地可同時耕作，且都能種植任何已解鎖植物。')
        if self.confirming_cancel:
            embed.insert_field_at(0, name='⚠️ 請確認',
                                  value='中斷後這塊田不會獲得作物或農耕 XP。', inline=False)
        for location_id, location_name in LOCATIONS.items():
            session = state['sessions'].get(location_id)
            if state['level'] < LOCATION_LEVELS[location_id]:
                value = f'農耕 Lv.{LOCATION_LEVELS[location_id]} 解鎖'
            elif not session or session['status'] != 'active':
                specialization = state['specializations'].get(location_id)
                value = '目前閒置' + (f'｜{SPECIALIZATIONS[specialization]}專精' if specialization else '')
            else:
                plant = PLANTS[session['plant_id']]
                ready = time.time() >= session['ready_at']
                value = (f'{plant.name}｜種植時 Lv.{session["level_snapshot"]}\n'
                         f'專精：{SPECIALIZATIONS.get(session.get("specialization"), "無")}\n'
                         f'背包持有 ×{inventory.get(plant.item_id, 0):,}\n') + (
                    '**已成熟，可以收成！**' if ready else f'<t:{int(session["ready_at"])}:R>成熟')
            embed.add_field(name=location_name, value=value, inline=True)
        selected = PLANTS[self.plant_id]
        embed.add_field(name='目前選擇', value=
                        f'{LOCATIONS[self.location_id]}｜{selected.name}\n'
                        f'背包持有 ×{inventory.get(selected.item_id, 0):,}\n'
                        f'成長 {growth_text(selected.seconds)}｜基礎收成 {selected.base_yield}｜每份 {selected.xp_each} XP\n'
                        f'料理：{selected.role}\n'
                        f'田地專精：{SPECIALIZATIONS.get(state["specializations"].get(self.location_id), "尚未設定")}', inline=False)
        embed.add_field(name='成熟通知', value='私訊通知已開啟' if state['notify'] else '私訊通知已關閉')
        if notice:
            embed.insert_field_at(0, name='最新狀態', value=notice[:1024], inline=False)
        embed.set_footer(text='每高於作物需求 10 級必定 +1 收成；不足 10 級的差距每級提供 10% 機率 +1，最多合計 +3。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('農耕面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'life':
                await navigate(self, interaction, 'life')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='農耕面板已關閉。', embed=None, view=None)
                return
            notice = None
            if self.confirming_cancel and action not in ('cancel', 'cancel_confirm', 'keep'):
                self.confirming_cancel = False
            try:
                if action == 'location' and value in LOCATIONS:
                    required_level = LOCATION_LEVELS[value]
                    if self.cog.farming.state(self.guild_id, self.owner.id)['level'] < required_level:
                        raise CharacterError(f'農耕 Lv.{required_level} 才能使用{LOCATIONS[value]}。')
                    self.location_id = value
                elif action == 'plant_choice' and value in PLANTS:
                    if self.cog.farming.state(self.guild_id, self.owner.id)['level'] < PLANTS[value].level:
                        raise CharacterError(f'農耕 Lv.{PLANTS[value].level} 才能種植{PLANTS[value].name}。')
                    self.plant_id = value
                elif action == 'specialization' and value in SPECIALIZATIONS:
                    self.cog.farming.set_specialization(
                        self.guild_id, self.owner.id, self.location_id, value)
                    notice = f'{LOCATIONS[self.location_id]}已設定為{SPECIALIZATIONS[value]}專精。'
                elif action == 'plant':
                    result = self.cog.farming.plant(
                        self.guild_id, self.owner.id, self.location_id, self.plant_id)
                    notice = f'已在{result["location"]}種下{result["plant"].name}。'
                    if result.get('specialization'):
                        notice += f'本批使用{SPECIALIZATIONS[result["specialization"]]}專精。'
                elif action == 'harvest':
                    result = self.cog.farming.harvest(self.guild_id, self.owner.id, self.location_id)
                    plant = PLANTS[result['plant_id']]
                    notice = (f'收成 {plant.name} ×{result["quantity"]}（基礎 {result["base_yield"]}'
                              f'、等級加成 {result["level_bonus"]}'
                              f'、專精加成 {result.get("specialization_bonus", 0)}），'
                              f'獲得 {result["xp"]:,} 農耕 XP。')
                    if result.get('training_bonus_xp'):
                        notice += f'（田地研習 +{result["training_bonus_xp"]:,} XP）'
                    if result.get('study_bonus_xp'):
                        notice += f'（人偶農藝研習 +{result["study_bonus_xp"]:,} XP）'
                    if result.get('tarot_bonus_xp'):
                        notice += f'（塔羅牌生活 XP +{result["tarot_bonus_xp"]:,}）'
                    if result.get('accessory_material'):
                        notice += f'\n額外獲得 {ITEMS["life:farming:star_fiber"].name} ×1！'
                    for location_id, required_level in LOCATION_LEVELS.items():
                        if required_level > 1 and result['old_level'] < required_level <= result['new_level']:
                            notice += f'\n解鎖新農地：{LOCATIONS[location_id]}！'
                    for unlocked in PLANTS.values():
                        if unlocked.level > 1 and result['old_level'] < unlocked.level <= result['new_level']:
                            notice += f'\n解鎖新植物：{unlocked.name}！'
                    if result['old_level'] < SPECIALIZATION_LEVEL <= result['new_level']:
                        notice += '\n已解鎖田地專精：可為每塊空田選擇豐收或研習！'
                    if result['new_level'] > result['old_level']:
                        notice += f'\n農耕等級提升至 Lv.{result["new_level"]}！'
                elif action == 'cancel':
                    self.confirming_cancel = True
                elif action == 'keep':
                    self.confirming_cancel = False
                elif action == 'cancel_confirm':
                    if not self.confirming_cancel:
                        raise CharacterError('請先確認要中斷的種植。')
                    result = self.cog.farming.cancel(self.guild_id, self.owner.id, self.location_id)
                    self.confirming_cancel = False
                    notice = (f'已中斷{LOCATIONS[result["location_id"]]}的{PLANTS[result["plant_id"]].name}種植；'
                              '本次不會獲得作物或農耕 XP。')
                elif action == 'notify':
                    state = self.cog.farming.state(self.guild_id, self.owner.id)
                    enabled = self.cog.farming.set_notify(self.guild_id, self.owner.id, not state['notify'])
                    notice = '植物成熟時會私訊通知。' if enabled else '已關閉植物成熟私訊。'
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
                await self.origin.edit_original_response(content='農耕面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
