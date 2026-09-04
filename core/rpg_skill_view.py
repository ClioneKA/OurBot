"""Private, immediately saved skill strategy controls."""
import asyncio

from core.rpg_menu import add_back, navigate

import discord

from core.rpg_battle import CONDITIONS, TARGETS, SKILLS, ALLY_EFFECTS
from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect


class SkillView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.job = cog.characters.job(self.guild_id, self.owner.id)
        self.slot = 1
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def current(self):
        return next(rule for rule in self.cog.tactics.rules(self.guild_id, self.owner.id, self.job) if rule.slot == self.slot)

    def rebuild(self):
        rule = self.current()
        skill = SKILLS[self.job][self.slot - 1]
        self.clear_items()
        self.add_item(PanelSelect('slot', row=0, placeholder='選擇要設定的技能', options=[
            discord.SelectOption(label=f'槽 {i}：{s.name}', value=str(i), default=i == self.slot)
            for i, s in enumerate(SKILLS[self.job], 1)]))
        self.add_item(PanelSelect('priority', row=1, placeholder='選擇優先順序', options=[
            discord.SelectOption(label=f'優先 {i}' + ('（最先）' if i == 1 else ''), value=str(i), default=i == rule.priority)
            for i in (1, 2, 3)]))
        self.add_item(PanelSelect('condition', row=2, placeholder='選擇施放條件', options=[
            discord.SelectOption(label=label, value=key, default=key == rule.condition) for key, label in CONDITIONS.items()]))
        fixed = {'guard': '固定目標：全隊', 'area': '固定目標：全體敵人',
                 'stance': '固定目標：自己', 'taunt': '固定目標：自己'}.get(skill.effect)
        targets = {key: label for key, label in TARGETS.items() if key != 'self' or skill.effect in ALLY_EFFECTS}
        self.add_item(PanelSelect('target', row=3, placeholder=fixed or '選擇目標規則', disabled=bool(fixed), options=[
            discord.SelectOption(label=fixed, value=rule.target, default=True)] if fixed else [
            discord.SelectOption(label=label, value=key, default=key == rule.target) for key, label in targets.items()]))
        self.toggle.label = '停用自動施放' if rule.enabled else '啟用自動施放'
        self.toggle.style = discord.ButtonStyle.secondary if rule.enabled else discord.ButtonStyle.success
        for button in (self.toggle, self.refresh, self.close_panel):
            self.add_item(button)
        add_back(self, 4)

    def embed(self, notice=None):
        embed = self.cog.skills_embed(self.guild_id, self.owner.id)
        embed.add_field(name='正在設定', value=f'槽 {self.slot}：{SKILLS[self.job][self.slot - 1].name}', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='選擇後立即保存，開戰時套用。優先順序交換不重複；閒置 3 分鐘後關閉，可重開 /冒險 → 技能。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 技能 開啟自己的技能面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險 → 技能。', ephemeral=True)
                return
            if action == 'home':
                await navigate(self, interaction)
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='技能面板已關閉。', embed=None, view=None)
                return
            job = self.cog.characters.job(self.guild_id, self.owner.id)
            notice = None
            if job != self.job:
                self.job, self.slot = job, 1
                notice = '職業已變更，已重新載入技能；請再次選擇設定。'
            else:
                try:
                    if action == 'slot':
                        if value not in ('1', '2', '3'):
                            raise CharacterError('無效的技能槽。')
                        self.slot = int(value)
                    elif action in ('priority', 'condition', 'target', 'toggle'):
                        old = self.current()  # Preserve other changes made in another panel.
                        if action == 'priority' and value not in ('1', '2', '3'):
                            raise CharacterError('無效的優先順序。')
                        if action == 'target' and SKILLS[self.job][self.slot - 1].effect in ('guard', 'area', 'stance', 'taunt'):
                            raise CharacterError('這個技能的目標固定，不需設定。')
                        self.cog.tactics.configure(self.guild_id, self.owner.id, self.job, self.slot,
                            int(value) if action == 'priority' else old.priority,
                            not old.enabled if action == 'toggle' else old.enabled,
                            value if action == 'condition' else old.condition,
                            value if action == 'target' else old.target)
                        notice = '已保存，開戰時套用。'
                except CharacterError as exc:
                    notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='停用自動施放', row=4)
    async def toggle(self, interaction, button):
        await self.handle(interaction, 'toggle')

    @discord.ui.button(label='重新整理', row=4)
    async def refresh(self, interaction, button):
        await self.handle(interaction, 'refresh')

    @discord.ui.button(label='關閉', row=4)
    async def close_panel(self, interaction, button):
        await self.handle(interaction, 'close')

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='技能面板已逾時，請重新使用 /冒險 → 技能。', view=None)
            except discord.HTTPException:
                pass
