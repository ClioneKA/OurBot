"""Private, immediately saved skill strategy controls."""
import asyncio

from core.rpg_menu import add_back, navigate

import discord

from core.rpg_battle import (CONDITIONS, CONDITION_LIMITS, TARGETS, ALLY_EFFECTS, FIXED_TARGETS,
                             condition_text, rule_skill)
from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect


class ConditionValueModal(discord.ui.Modal):
    def __init__(self, panel, condition):
        super().__init__(title='設定施放條件')
        self.panel, self.condition = panel, condition
        low, high, default, suffix = CONDITION_LIMITS[condition]
        rule = panel.current()
        current = rule.condition_value if rule.condition == condition else default
        self.threshold = discord.ui.TextInput(
            label=f'{CONDITIONS[condition]}（{low}–{high}{suffix}）'[:45],
            default=str(current), min_length=1, max_length=3)
        self.add_item(self.threshold)

    async def on_submit(self, interaction):
        try:
            value = int(self.threshold.value)
        except ValueError:
            await interaction.response.send_message('請輸入範圍內的正整數。', ephemeral=True)
            return
        await self.panel.handle(interaction, 'condition_value', (self.condition, value))


class SkillView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.job = cog.characters.job(self.guild_id, self.owner.id)
        self.slot = 1
        self.choosing_skill = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def current(self):
        return next(rule for rule in self.cog.tactics.rules(self.guild_id, self.owner.id, self.job) if rule.slot == self.slot)

    def rebuild(self):
        rule = self.current()
        skill = rule_skill(self.job, rule)
        self.clear_items()
        self.add_item(PanelSelect('slot', row=0, placeholder='選擇要設定的技能', options=[
            discord.SelectOption(label=f'槽 {i}：{s.name}', value=str(i), default=i == self.slot)
            for i, s in sorted((r.slot, rule_skill(self.job, r))
                               for r in self.cog.tactics.rules(self.guild_id, self.owner.id, self.job))]))
        if self.choosing_skill:
            available = self.cog.tactics.available(self.guild_id, self.owner.id, self.job)
            self.add_item(PanelSelect('equip', row=1, placeholder='選擇已解鎖技能', options=[
                discord.SelectOption(label=s.name, value=str(i), description=s.description[:100],
                                     default=i == (rule.skill_id or rule.slot))
                for i, s in enumerate(available, 1)]))
            self.change_skill.label = '返回策略設定'
            for button in (self.change_skill, self.refresh, self.close_panel):
                self.add_item(button)
            add_back(self, 4)
            return
        self.change_skill.label = '更換技能'
        self.add_item(PanelSelect('priority', row=1, placeholder='選擇優先順序', options=[
            discord.SelectOption(label=f'優先 {i}' + ('（最先）' if i == 1 else ''), value=str(i), default=i == rule.priority)
            for i in (1, 2, 3)]))
        self.add_item(PanelSelect('condition', row=2, placeholder='選擇施放條件', options=[
            discord.SelectOption(label=condition_text(key, rule.condition_value) if key == rule.condition else label,
                                 value=key, default=key == rule.condition)
            for key, label in CONDITIONS.items()]))
        fixed = f'固定目標：{FIXED_TARGETS[skill.effect]}' if skill.effect in FIXED_TARGETS else None
        targets = {key: label for key, label in TARGETS.items() if key != 'self' or skill.effect in ALLY_EFFECTS}
        if skill.effect != 'cleanse':
            targets.pop('debuffed', None)
        self.add_item(PanelSelect('target', row=3, placeholder=fixed or '選擇目標規則', disabled=bool(fixed), options=[
            discord.SelectOption(label=fixed, value=rule.target, default=True)] if fixed else [
            discord.SelectOption(label=label, value=key, default=key == rule.target) for key, label in targets.items()]))
        self.toggle.label = '停用自動施放' if rule.enabled else '啟用自動施放'
        self.toggle.style = discord.ButtonStyle.secondary if rule.enabled else discord.ButtonStyle.success
        for button in (self.change_skill, self.toggle, self.refresh, self.close_panel):
            self.add_item(button)
        add_back(self, 4)

    def embed(self, notice=None):
        embed = self.cog.skills_embed(self.guild_id, self.owner.id)
        skill = rule_skill(self.job, self.current())
        embed.add_field(name='正在設定', value=f'槽 {self.slot}：{skill.name}', inline=False)
        if self.choosing_skill:
            embed.add_field(name='更換技能', value='Lv.20 解鎖兩個進階技能；維持三格，同技能不能重複裝備。'
                            '更換後保留該格順位與開關，施放條件及目標恢復新技能預設值。', inline=False)
        if skill.effect == 'cleanse':
            embed.add_field(name='淨化目標規則', value='只選存活且中毒／破甲／暈眩的隊友（含自己）。選「有可淨化負面狀態的隊友」時，多人符合則選血量比例最低者；其他選項依原規則篩選。'
                            '若希望先解除狀態，可把淨化設為優先 1；每回合只施放一個技能。', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='數值條件會開啟輸入視窗，其餘選擇後立即保存；開戰時套用。閒置 3 分鐘後關閉。')
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
                self.choosing_skill = False
                notice = '職業已變更，已重新載入技能；請再次選擇設定。'
            else:
                try:
                    if action == 'change_skill':
                        self.choosing_skill = not self.choosing_skill
                    elif action == 'equip':
                        if value not in tuple(str(i) for i in range(1, 6)):
                            raise CharacterError('無效的技能。')
                        self.cog.tactics.equip(self.guild_id, self.owner.id, self.job, self.slot, int(value))
                        self.choosing_skill = False
                        notice = '已更換技能，開戰時套用。'
                    elif action == 'slot':
                        if value not in ('1', '2', '3'):
                            raise CharacterError('無效的技能槽。')
                        self.slot = int(value)
                    elif action == 'condition' and value in CONDITION_LIMITS:
                        await interaction.response.send_modal(ConditionValueModal(self, value))
                        return
                    elif action in ('priority', 'condition', 'condition_value', 'target', 'toggle'):
                        old = self.current()  # Preserve other changes made in another panel.
                        if action == 'priority' and value not in ('1', '2', '3'):
                            raise CharacterError('無效的優先順序。')
                        if action == 'condition_value':
                            if not isinstance(value, tuple) or len(value) != 2:
                                raise CharacterError('無效的施放條件數值。')
                            selected_condition, threshold = value
                        else:
                            selected_condition = value if action == 'condition' else old.condition
                            threshold = old.condition_value if selected_condition == old.condition else None
                        if action == 'target' and rule_skill(self.job, old).effect in FIXED_TARGETS:
                            raise CharacterError('這個技能的目標固定，不需設定。')
                        self.cog.tactics.configure(self.guild_id, self.owner.id, self.job, self.slot,
                            int(value) if action == 'priority' else old.priority,
                            not old.enabled if action == 'toggle' else old.enabled,
                            selected_condition, value if action == 'target' else old.target, threshold)
                        notice = '已保存，開戰時套用。'
                except CharacterError as exc:
                    notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    @discord.ui.button(label='更換技能', row=4)
    async def change_skill(self, interaction, button):
        await self.handle(interaction, 'change_skill')

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
