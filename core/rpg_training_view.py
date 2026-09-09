"""Private training panel; each run reads the player's current loadout."""
from dataclasses import asdict
from io import BytesIO

import discord

from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import AdventureView
from core.rpg_training import COUNT_OPTIONS, DEFENSE_OPTIONS, ROUND_OPTIONS, train


class TrainingView(AdventureView):
    def __init__(self, cog, interaction):
        self.rounds, self.count, self.defense = 10, 1, 0
        self.battle = None
        self.damage_by_round = []
        super().__init__(cog, interaction, page='training')

    def rebuild(self):
        self.clear_items()
        for row, (action, label, values) in enumerate((
                ('rounds', '回合', ROUND_OPTIONS), ('count', '假人數量', COUNT_OPTIONS),
                ('defense', '假人防禦', DEFENSE_OPTIONS))):
            self.add_item(PanelSelect(action, row=row, placeholder=label, options=[
                discord.SelectOption(label=f'{label}：{value}', value=str(value),
                                     default=value == getattr(self, action))
                for value in values]))
        self.button('開始測試／重新測試', 'run', 3)
        self.button('下載戰鬥紀錄', 'log', 3, self.battle is None)
        self.button('裝備／能力', 'equipment', 4)
        self.button('技能', 'skills', 4)
        self.button('出戰配置', 'loadouts', 4)
        self.button('返回主選單', 'home', 4)
        self.button('關閉', 'close', 4)

    def embed(self, notice=None):
        embed = discord.Embed(title='訓練假人｜配裝測試', color=0xF59E0B,
            description=(f'{self.count} 隻假人・防禦 {self.defense}・{self.rounds} 回合\n'
                         '每次測試讀取目前裝備、技能條件與職業被動。\n'
                         '假人不反擊、閃避為 0、受傷後立即回滿；第一隻視為首領。\n'
                         '不消耗道具、不發放獎勵；不套用料理、藥水、酒館或占卜效果。\n'
                         '此模式測試輸出；受擊、低血量及隊友相關效果不一定能觸發。'))
        if self.battle:
            actor = self.battle.fighters[0]
            stats = actor.combat_stats
            embed.add_field(name=f'測試結果｜{actor.job}', inline=False,
                value=(f'總傷害 **{stats["damage_dealt"]:,}**\n'
                       f'平均每回合 **{stats["damage_dealt"] / self.battle.round:,.1f}**\n'
                       f'直接傷害 {stats["direct_damage"]:,}・追加／持續傷害 {stats["support_damage"]:,}\n'
                       f'命中 {stats["hits"]}・未命中 {stats["misses"]}・暴擊 {stats["critical_hits"]}'))
            embed.add_field(name='每回合傷害', inline=False,
                value=' / '.join(f'{value:,}' for value in self.damage_by_round)[:1024])
            embed.add_field(name='技能使用次數', inline=False,
                value='、'.join(f'{name} ×{count}' for name, count in stats['skills_used'].items()) or '無')
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='每次測試重新隨機判定；實戰傷害仍受敵人與隊伍影響。')
        return embed

    async def handle(self, interaction, action, value=None):
        if action not in ('run', 'log', 'rounds', 'count', 'defense'):
            return await super().handle(interaction, action, value)
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險。', ephemeral=True)
                return
            notice = None
            try:
                if action == 'run':
                    state = self.cog.characters.snapshot(self.guild_id, self.owner.id)
                    tactics = self.cog.tactics
                    passive = tactics.passive(self.guild_id, self.owner.id, state['job'])
                    participant = dict(id=self.owner.id, name=self.owner.display_name, state=state,
                        rules=[asdict(rule) for rule in tactics.rules(self.guild_id, self.owner.id, state['job'])],
                        basic_target=tactics.basic_target(self.guild_id, self.owner.id, state['job']),
                        passive_id=passive.id if passive else None)
                    self.battle, self.damage_by_round = train(
                        participant, rounds=self.rounds, count=self.count, defense=self.defense)
                elif action == 'log':
                    if self.battle is None:
                        raise CharacterError('請先開始測試。')
                    header = (f'訓練假人：{self.count} 隻，防禦 {self.defense}，'
                              f'{self.rounds} 回合\n')
                    data = (header + '\n'.join(self.battle.log)).encode('utf-8')
                    await interaction.response.send_message(
                        file=discord.File(BytesIO(data), filename='training-battle.txt'), ephemeral=True)
                    return
                else:
                    options = {'rounds': ROUND_OPTIONS, 'count': COUNT_OPTIONS, 'defense': DEFENSE_OPTIONS}[action]
                    if value not in tuple(map(str, options)):
                        raise CharacterError('無效的訓練假人設定。')
                    setattr(self, action, int(value))
                    self.battle = None
                    self.damage_by_round = []
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self,
                allowed_mentions=discord.AllowedMentions.none())
