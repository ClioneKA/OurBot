"""Private Discord panel for two concurrent farming plots."""
import asyncio
import time

import discord

from core.rpg import MAX_LEVEL
from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect
from core.rpg_farming import LOCATION_LEVELS, LOCATIONS, PLANTS, farming_progress, growth_text
from core.rpg_menu import navigate


class FarmingView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.location_id, self.plant_id = 'courtyard', 'potato'
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
                                 description=f'Lv.{PLANTS[key].level}｜{growth_text(PLANTS[key].seconds)}｜{PLANTS[key].role}',
                                 default=key == self.plant_id) for key in unlocked]))
        session = state['sessions'].get(self.location_id)
        active = bool(session and session['status'] == 'active')
        ready = bool(active and time.time() >= session['ready_at'])
        self._button('種植', 'plant', 2, active, discord.ButtonStyle.success)
        self._button('收成', 'harvest', 2, not ready, discord.ButtonStyle.primary)
        self._button('關閉成熟通知' if state['notify'] else '開啟成熟通知', 'notify', 2)
        self._button('返回生活', 'life', 3)
        self._button('重新整理', 'refresh', 3)
        self._button('關閉', 'close', 3)
        return state

    def embed(self, notice=None):
        state = self.cog.farming.state(self.guild_id, self.owner.id)
        level, progress, required = farming_progress(state['xp'])
        xp_text = (f'已達最高等級 Lv.{MAX_LEVEL}｜累積 {state["xp"]:,} XP' if required is None
                   else f'{progress:,}／{required:,} XP｜累積 {state["xp"]:,} XP')
        embed = discord.Embed(title='安安大冒險｜農耕', color=0x65A30D,
                              description=f'農耕 Lv.**{level}**｜{xp_text}\n兩塊田地可同時耕作，且都能種植任何已解鎖植物。')
        for location_id, location_name in LOCATIONS.items():
            session = state['sessions'].get(location_id)
            if state['level'] < LOCATION_LEVELS[location_id]:
                value = f'農耕 Lv.{LOCATION_LEVELS[location_id]} 解鎖'
            elif not session or session['status'] == 'harvested':
                value = '目前閒置'
            else:
                plant = PLANTS[session['plant_id']]
                ready = time.time() >= session['ready_at']
                value = f'{plant.name}｜種植時 Lv.{session["level_snapshot"]}\n' + (
                    '**已成熟，可以收成！**' if ready else f'<t:{int(session["ready_at"])}:R>成熟')
            embed.add_field(name=location_name, value=value, inline=True)
        selected = PLANTS[self.plant_id]
        embed.add_field(name='目前選擇', value=
                        f'{LOCATIONS[self.location_id]}｜{selected.name}\n'
                        f'成長 {growth_text(selected.seconds)}｜基礎收成 {selected.base_yield}｜每份 {selected.xp_each} XP', inline=False)
        embed.add_field(name='成熟通知', value='私訊通知已開啟' if state['notify'] else '私訊通知已關閉')
        if notice:
            embed.add_field(name='操作結果', value=notice[:1024], inline=False)
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
                elif action == 'plant':
                    result = self.cog.farming.plant(
                        self.guild_id, self.owner.id, self.location_id, self.plant_id)
                    notice = f'已在{result["location"]}種下{result["plant"].name}。'
                elif action == 'harvest':
                    result = self.cog.farming.harvest(self.guild_id, self.owner.id, self.location_id)
                    plant = PLANTS[result['plant_id']]
                    notice = (f'收成 {plant.name} ×{result["quantity"]}（基礎 {result["base_yield"]}'
                              f'、等級加成 {result["level_bonus"]}），獲得 {result["xp"]:,} 農耕 XP。')
                    if result['new_level'] > result['old_level']:
                        notice += f'\n農耕等級提升至 Lv.{result["new_level"]}！'
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
