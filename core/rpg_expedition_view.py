"""Private expedition panel with session-bound cancellation confirmation."""
import time

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_equipment_view import PanelSelect
from core.rpg_expeditions import DURATIONS, EXPEDITION_ROUTES
from core.rpg_menu import AdventureView, add_back, add_favorite_toggle, navigate


def rewards(result):
    if result.get('kind') == 'doll':
        if result.get('material'):
            return f'{ITEMS[result["material"]].name} ×{result["quantity"]}'
        return f'{result["gold"]:,} 金幣'
    return f'討伐之證 ×{result["proofs"]}｜{result["xp"]:,} XP｜{result["gold"]:,} 金幣'


class ExpeditionView(AdventureView):
    def __init__(self, cog, interaction):
        self.hours = 1
        self.route = 'gold'
        self.session_id = None
        self.confirm_id = None
        super().__init__(cog, interaction, 'expedition')

    def rebuild(self):
        self.clear_items()
        session = self.cog.expeditions.state(self.guild_id, self.owner.id)
        self.session_id = session['id'] if session else None
        ready = bool(session and time.time() >= session['ready_at'])
        local = not session or session['guild_id'] == self.guild_id
        legacy = bool(session and session.get('kind') != 'doll')
        if self.confirm_id and (not session or session['id'] != self.confirm_id or ready):
            self.confirm_id = None
        if self.confirm_id:
            self.button('繼續遠征', 'keep', 0)
            self.button('確認中斷並放棄全部獎勵', 'cancel_confirm', 0)
        elif not session:
            self.add_item(PanelSelect('route', row=0, placeholder='選擇人偶遠征', options=[
                discord.SelectOption(label=name, value=key, default=key == self.route)
                for key, (name, _stat) in EXPEDITION_ROUTES.items()]))
            self.add_item(PanelSelect('duration', row=1, placeholder='選擇遠征時間', options=[
                discord.SelectOption(label=f'{hours} 小時', value=str(hours), default=hours == self.hours)
                for hours in DURATIONS]))
            self.button('派遣人偶', 'start', 2)
        else:
            self.button('領取獎勵', 'claim', 1, not ready or not local)
            if not legacy:
                self.button('領取並再次遠征', 'repeat', 1, not ready or not local)
            self.button('中斷遠征', 'cancel', 1, ready or not local)
        add_back(self, 4, 'alchemy:overview', '返回煉金人偶')
        add_favorite_toggle(self, 4, 'expedition')
        self.button('重新整理', 'refresh', 4)
        self.button('關閉', 'close', 4)

    def embed(self, notice=None):
        session = self.cog.expeditions.state(self.guild_id, self.owner.id)
        embed = discord.Embed(title='煉金人偶｜人偶遠征', color=0x527A70,
            description='遠征期間人偶不能進行生活自動化或戰鬥支援，玩家仍可正常活動。\n'
                        '燃料在出發時扣除；中斷不退還燃料且本趟獎勵歸零。')
        if session:
            ready = time.time() >= session['ready_at']
            legacy = session.get('kind') != 'doll'
            heading = '舊制遠征' if legacy else session['route_name']
            detail = (f'{session["hours"]} 小時｜出發時 Lv.{session["level"]}（{session["tier"]} 階）'
                      if legacy else
                      f'{session["hours"]} 小時｜素體 T{session["body_tier"]}｜消耗 {session["fuel"]} 燃料')
            embed.add_field(name='已返回，等待領獎' if ready else '遠征進行中', inline=False,
                value=f'**{heading}**｜{detail}\n'
                      f'出發：<t:{int(session["started_at"])}:f>\n'
                      f'返回：<t:{int(session["ready_at"])}:f>（<t:{int(session["ready_at"])}:R>）\n'
                      + rewards(session))
            if session['guild_id'] != self.guild_id:
                embed.add_field(name='其他伺服器的遠征', value='請回到出發的伺服器領取或中斷。', inline=False)
            if self.confirm_id:
                embed.add_field(name='確認中斷', value='現在返回將失去上列全部獎勵，已經過的時間不會保留。', inline=False)
        else:
            try:
                for hours in DURATIONS:
                    result = self.cog.expeditions.preview(
                        self.guild_id, self.owner.id, hours, self.route)
                    fuel_status = (f'出發後剩餘 {result["remaining_fuel"]} 燃料'
                                   if result['remaining_fuel'] >= 0 else
                                   f'燃料不足（尚缺 {-result["remaining_fuel"]}）')
                    embed.add_field(
                        name=f'{result["route_name"]}・{hours} 小時'
                             + ('（已選擇）' if hours == self.hours else ''),
                        value=f'{rewards(result)}｜消耗 {result["fuel"]} 燃料'
                              f'｜{fuel_status}', inline=False)
            except CharacterError as exc:
                embed.add_field(name='目前不能派遣', value=str(exc), inline=False)
            embed.add_field(name='預計返回', value=f'<t:{int(time.time() + self.hours * 3600)}:f>', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='獎勵、素材階級與燃料在出發時固定；遠征到期後人偶立即恢復使用。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'alchemy':
                await navigate(self, interaction, 'alchemy')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='遠征面板已關閉。', embed=None, view=None)
                return
            notice = None
            try:
                if action == 'duration':
                    self.hours = int(value)
                elif action == 'route':
                    self.route = value
                elif action == 'start':
                    self.cog.expeditions.start(
                        self.guild_id, self.owner.id, self.hours, self.route)
                    notice = '人偶已出發，離線期間會繼續計時。'
                elif action == 'cancel':
                    self.confirm_id = self.session_id
                elif action == 'keep':
                    self.confirm_id = None
                elif action == 'cancel_confirm':
                    if not self.confirm_id:
                        raise CharacterError('請先確認要中斷的遠征。')
                    self.cog.expeditions.finish(self.guild_id, self.owner.id, self.confirm_id, cancel=True)
                    self.confirm_id = None
                    notice = '已中斷人偶遠征；燃料不退還，本趟沒有獎勵。'
                elif action in ('claim', 'repeat'):
                    result = self.cog.expeditions.finish(self.guild_id, self.owner.id, self.session_id)
                    notice = '已領取：' + rewards(result)
                    if action == 'repeat':
                        try:
                            self.cog.expeditions.start(
                                self.guild_id, self.owner.id, result['hours'], result['route'])
                            notice += '。已再次出發。'
                        except CharacterError as exc:
                            notice += f'。獎勵已入帳；再次出發失敗：{exc}'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())
