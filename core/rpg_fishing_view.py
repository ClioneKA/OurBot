"""Private Discord panel for fishing dispatches and rod progression."""
import asyncio
import time

import discord

from core.rpg import MAX_LEVEL
from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_fishing import DURATIONS, RECIPES, ROD_BONUS, SPOTS, fishing_mastery, fishing_progress
from core.rpg_menu import navigate


class FishingView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.spot_id, self.duration_id = 'pond', 'short'
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 生活 → 釣魚 開啟自己的面板。', ephemeral=True)
            return False
        return True

    def _button(self, label, action, row, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        state = self.cog.fishing.state(self.guild_id, self.owner.id)
        if state['level'] < SPOTS[self.spot_id].level:
            self.spot_id = 'pond'
        self.clear_items()
        self.add_item(PanelSelect('spot', row=0, placeholder='選擇釣場', options=[
            discord.SelectOption(label=spot.name, value=key,
                description=f'釣魚 Lv.{spot.level}｜每次捕獲 {spot.base_xp} XP',
                default=key == self.spot_id)
            for key, spot in SPOTS.items() if state['level'] >= spot.level]))
        self.add_item(PanelSelect('duration', row=1, placeholder='選擇釣魚時間', options=[
            discord.SelectOption(label=label, value=key, description=f'基礎捕獲 {catches} 次',
                                 default=key == self.duration_id)
            for key, (label, _, catches) in DURATIONS.items()]))
        rods = [key for key in ROD_BONUS
                if self.cog.characters.inventory_counts(self.guild_id, self.owner.id).get(key, 0)]
        self.add_item(PanelSelect('rod', row=2, placeholder='選擇釣竿', options=[
            discord.SelectOption(label=ITEMS[key].name, value=key,
                                 description=ITEMS[key].description[:100], default=key == state['rod_id'])
            for key in rods]))
        session = state['session']
        active = session and session['status'] == 'active'
        ready = active and time.time() >= session['ready_at']
        current = state['rod_id']
        target = ('fishing:rod:simple' if current == 'fishing:rod:old' else
                  'fishing:rod:magic' if current == 'fishing:rod:simple' else None)
        self._button('開始釣魚', 'start', 3, bool(active), discord.ButtonStyle.success)
        self._button('收竿', 'claim', 3, not ready, discord.ButtonStyle.primary)
        self._button('中斷釣魚', 'cancel', 3, not active, discord.ButtonStyle.danger)
        self._button(f'製作{ITEMS[target].name}' if target else '已是最高階釣竿', 'craft', 3, not target)
        self._button('關閉完成通知' if state['notify'] else '開啟完成通知', 'notify', 3)
        self._button('返回生活', 'life', 4)
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)
        return state

    def embed(self, notice=None):
        state = self.cog.fishing.state(self.guild_id, self.owner.id)
        level, progress, required = fishing_progress(state['xp'])
        if required is None:
            xp_text = f'已達最高等級 Lv.{MAX_LEVEL}｜累積 {state["xp"]:,} XP'
        else:
            xp_text = f'{progress:,}／{required:,} XP｜累積 {state["xp"]:,} XP'
        embed = discord.Embed(title='安安大冒險｜釣魚', color=0x38BDF8,
            description=f'釣魚 Lv.**{level}**｜{xp_text}\n目前釣竿：**{ITEMS[state["rod_id"]].name}**\n{ITEMS[state["rod_id"]].description}')
        session = state['session']
        if session and session['status'] == 'active':
            ready = time.time() >= session['ready_at']
            embed.add_field(name='正在釣魚', value=
                f'{SPOTS[session["spot_id"]].name}｜{DURATIONS[session["duration_id"]][0]}\n'
                f'使用 {ITEMS[session["rod_id"]].name}｜基礎捕獲 {session["base_catches"]} 次\n'
                f'出發時 Lv.{session["level_snapshot"]}｜熟練產量 '
                f'{fishing_mastery(session["level_snapshot"], SPOTS[session["spot_id"]])}%\n'
                + ('**可以收竿了！**' if ready else f'<t:{int(session["ready_at"])}:R>完成'), inline=False)
        else:
            embed.add_field(name='準備釣魚', value=
                f'{SPOTS[self.spot_id].name}｜{DURATIONS[self.duration_id][0]}｜'
                f'基礎捕獲 {DURATIONS[self.duration_id][2]} 次\n'
                f'目前熟練產量：{fishing_mastery(level, SPOTS[self.spot_id])}%', inline=False)
        current = state['rod_id']
        target = ('fishing:rod:simple' if current == 'fishing:rod:old' else
                  'fishing:rod:magic' if current == 'fishing:rod:simple' else None)
        if target:
            counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
            recipe = '\n'.join(f'{ITEMS[key].name}：{counts.get(key, 0)}/1' for key in RECIPES[target])
            embed.add_field(name=f'下一階：{ITEMS[target].name}', value=recipe, inline=False)
        embed.add_field(name='完成通知', value='私訊通知已開啟' if state['notify'] else '私訊通知已關閉')
        if notice:
            embed.add_field(name='操作結果', value=notice[:1024], inline=False)
        embed.set_footer(text='釣完後不會自動重新開始；必須收竿後再次開始釣魚。')
        return embed

    def _claim_notice(self, result):
        lines = [f'{ITEMS[key].name} ×{count}' for key, count in result['items'].items()]
        header = f'已從{SPOTS[result["spot_id"]].name}收竿，共捕獲 {result["catches"]} 次。'
        if result['bonus']:
            header += '\n釣竿效果發動：追加一次捕獲！'
        if result.get('mastery_bonus', 0):
            header += f'\n熟練產量發動：額外獲得 {result["mastery_bonus"]} 份物品！'
        text = header + '\n' + '\n'.join(lines) + f'\n獲得 {result["xp"]:,} 釣魚 XP。'
        if result['old_level'] < 20 <= result['new_level']:
            text += '\n解鎖新釣場：魔女島湖泊！'
        if result['new_level'] > result['old_level']:
            text += f'\n釣魚等級提升至 Lv.{result["new_level"]}！'
        return text

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('釣魚面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'life':
                await navigate(self, interaction, 'life')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='釣魚面板已關閉。', embed=None, view=None)
                return
            notice = None
            try:
                if action == 'spot' and value in SPOTS:
                    if self.cog.fishing.state(self.guild_id, self.owner.id)['level'] < SPOTS[value].level:
                        raise CharacterError(f'釣魚 Lv.{SPOTS[value].level} 才能前往{SPOTS[value].name}。')
                    self.spot_id = value
                elif action == 'duration' and value in DURATIONS:
                    self.duration_id = value
                elif action == 'rod':
                    item = self.cog.fishing.equip(self.guild_id, self.owner.id, value)
                    notice = f'已換用 {item.name}；這次釣魚仍使用出發時的釣竿。'
                elif action == 'start':
                    result = self.cog.fishing.start(self.guild_id, self.owner.id,
                                                    self.spot_id, self.duration_id)
                    notice = (f'已前往{result["spot"].name}，預計捕獲 {result["base_catches"]} 次；'
                              f'本次熟練產量為 {result["mastery_percent"]}%。')
                elif action == 'claim':
                    notice = self._claim_notice(self.cog.fishing.claim(self.guild_id, self.owner.id))
                elif action == 'cancel':
                    result = self.cog.fishing.cancel(self.guild_id, self.owner.id)
                    notice = (f'已中斷在{SPOTS[result["spot_id"]].name}的釣魚行程；'
                              '本次不會獲得任何物品或釣魚 XP。')
                elif action == 'craft':
                    item = self.cog.fishing.craft_next(self.guild_id, self.owner.id)
                    notice = f'成功製作 {item.name}，並已自動換上。'
                elif action == 'notify':
                    state = self.cog.fishing.state(self.guild_id, self.owner.id)
                    enabled = self.cog.fishing.set_notify(self.guild_id, self.owner.id, not state['notify'])
                    notice = '釣魚完成時會私訊通知。' if enabled else '已關閉釣魚完成私訊。'
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
                await self.origin.edit_original_response(content='釣魚面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
