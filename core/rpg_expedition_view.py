"""Private expedition panel with session-bound cancellation confirmation."""
import time

import discord

from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect
from core.rpg_expeditions import DURATIONS
from core.rpg_menu import AdventureView, navigate


def rewards(result):
    return f'討伐之證 ×{result["proofs"]}｜{result["xp"]:,} XP｜{result["gold"]:,} 金幣'


class ExpeditionView(AdventureView):
    def __init__(self, cog, interaction):
        self.hours = 4
        self.session_id = None
        self.confirm_id = None
        super().__init__(cog, interaction, 'expedition')

    def rebuild(self):
        self.clear_items()
        session = self.cog.expeditions.state(self.owner.id)
        self.session_id = session['id'] if session else None
        ready = bool(session and time.time() >= session['ready_at'])
        local = not session or session['guild_id'] == self.guild_id
        if self.confirm_id and (not session or session['id'] != self.confirm_id or ready):
            self.confirm_id = None
        if self.confirm_id:
            self.button('繼續遠征', 'keep', 0)
            self.button('確認中斷並放棄全部獎勵', 'cancel_confirm', 0)
        elif not session:
            self.add_item(PanelSelect('duration', row=0, placeholder='選擇遠征時間', options=[
                discord.SelectOption(label=f'{hours} 小時', value=str(hours), default=hours == self.hours)
                for hours in DURATIONS]))
            self.button('出發遠征', 'start', 1)
        else:
            self.button('領取獎勵', 'claim', 1, not ready or not local)
            self.button('領取並再次遠征', 'repeat', 1, not ready or not local)
            self.button('中斷遠征', 'cancel', 1, ready or not local)
        self.button('返回生活', 'life', 2)
        self.button('重新整理', 'refresh', 2)
        self.button('關閉', 'close', 2)

    def embed(self, notice=None):
        session = self.cog.expeditions.state(self.owner.id)
        embed = discord.Embed(title='安安大冒險｜遠征', color=0x527A70,
            description='遠征中不能參加討伐、總力戰或繪境迷廊；釣魚、農耕與聊天照常。\n'
                        '可隨時中斷，但本趟全部獎勵歸零。完成後即可參戰，不必先領獎。')
        if session:
            ready = time.time() >= session['ready_at']
            embed.add_field(name='已返回，等待領獎' if ready else '遠征進行中', inline=False,
                value=f'{session["hours"]} 小時｜出發時 Lv.{session["level"]}（{session["tier"]} 階）\n'
                      f'出發：<t:{int(session["started_at"])}:f>\n'
                      f'返回：<t:{int(session["ready_at"])}:f>（<t:{int(session["ready_at"])}:R>）\n'
                      + rewards(session))
            if session['guild_id'] != self.guild_id:
                embed.add_field(name='其他伺服器的遠征', value='請回到出發的伺服器領取或中斷。', inline=False)
            if self.confirm_id:
                embed.add_field(name='確認中斷', value='現在返回將失去上列全部獎勵，已經過的時間不會保留。', inline=False)
        else:
            for hours in DURATIONS:
                result = self.cog.expeditions.preview(self.guild_id, self.owner.id, hours)
                embed.add_field(name=f'{hours} 小時' + ('（已選擇）' if hours == self.hours else ''),
                                value=rewards(result), inline=False)
            embed.add_field(name='預計返回', value=f'<t:{int(time.time() + self.hours * 3600)}:f>', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='報酬依出發時等級與設定固定；不掉裝備、不消耗料理，不自動續跑。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            if action == 'life':
                await navigate(self, interaction, 'life')
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
                elif action == 'start':
                    self.cog.expeditions.start(self.guild_id, self.owner.id, self.hours)
                    notice = '已出發，離線期間會繼續計時。'
                elif action == 'cancel':
                    self.confirm_id = self.session_id
                elif action == 'keep':
                    self.confirm_id = None
                elif action == 'cancel_confirm':
                    if not self.confirm_id:
                        raise CharacterError('請先確認要中斷的遠征。')
                    self.cog.expeditions.finish(self.guild_id, self.owner.id, self.confirm_id, cancel=True)
                    self.confirm_id = None
                    notice = '已中斷遠征，本趟沒有獎勵。現在可以參加討伐。'
                elif action in ('claim', 'repeat'):
                    result = self.cog.expeditions.finish(self.guild_id, self.owner.id, self.session_id)
                    notice = '已領取：' + rewards(result)
                    if action == 'repeat':
                        try:
                            self.cog.expeditions.start(self.guild_id, self.owner.id, result['hours'])
                            notice += '。已再次出發。'
                        except CharacterError as exc:
                            notice += f'。獎勵已入帳；再次出發失敗：{exc}'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())
