"""Private controls for saving and applying complete combat loadouts."""
import asyncio

import discord

from core.rpg_battle import Rule, TARGETS, FIXED_TARGETS, condition_text, passive_description, rule_skill
from core.rpg_character import CharacterError
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import add_back, navigate


class RenameLoadoutModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='重新命名出戰配置')
        self.panel = panel
        profile = panel.current()
        self.name = discord.ui.TextInput(label='配置名稱', default=profile['name'],
                                         min_length=1, max_length=20)
        self.add_item(self.name)

    async def on_submit(self, interaction):
        await self.panel.handle(interaction, 'rename_value', self.name.value)


class LoadoutView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.slot = 1
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def current(self):
        return self.cog.loadouts.get(self.guild_id, self.owner.id, self.slot)

    def rebuild(self):
        profiles = self.cog.loadouts.all(self.guild_id, self.owner.id)
        self.clear_items()
        self.add_item(PanelSelect('slot', row=0, placeholder='選擇出戰配置', options=[
            discord.SelectOption(label=profile['name'], value=str(profile['slot']),
                                 description='尚未保存' if profile['data'] is None else profile['data']['job'],
                                 default=profile['slot'] == self.slot)
            for profile in profiles]))
        empty = self.current()['data'] is None
        for label, action, style, disabled in (
                ('保存目前配置', 'save', discord.ButtonStyle.success, False),
                ('套用配置', 'apply', discord.ButtonStyle.primary, empty),
                ('重新命名', 'rename', discord.ButtonStyle.secondary, False),
                ('清空配置', 'clear', discord.ButtonStyle.danger, empty)):
            button = discord.ui.Button(label=label, row=1, style=style, disabled=disabled)
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)
        add_back(self, 4)
        for label, action in (('重新整理', 'refresh'), ('關閉', 'close')):
            button = discord.ui.Button(label=label, row=4)
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)

    def embed(self, notice=None):
        profile = self.current()
        data = profile['data']
        if data is None:
            description = '尚未保存。請先調整職業、裝備與技能，再按「保存目前配置」。'
        else:
            equipment = []
            for slot, instance_id in data.get('equipment', {}).items():
                instance = self.cog.characters.get_instance(self.guild_id, self.owner.id, instance_id)
                name = self.cog.characters.resolved_item(instance).name if instance else '物品已遺失'
                equipment.append(f'{slot}：{name} #{instance_id}')
            rules = []
            for raw in sorted(data.get('rules', []), key=lambda entry: entry.get('priority', 99)):
                try:
                    rule = Rule(**raw)
                    skill = rule_skill(data['job'], rule)
                    status = '' if rule.enabled else '【停用】'
                    target = FIXED_TARGETS.get(skill.effect, TARGETS[rule.target])
                    rules.append(f'{rule.priority}. {status}{skill.name}｜{condition_text(rule.condition, rule.condition_value)}｜{target}')
                except (KeyError, TypeError, IndexError, CharacterError):
                    rules.append('技能資料已失效')
            passive = next((passive for passive in self.cog.tactics.available_passives(
                self.guild_id, self.owner.id, data['job']) if passive.id == data.get('passive_id')), None)
            passive_line = f'{passive.name}：{passive_description(passive)}' if passive else '未裝備'
            description = (f'職業：**{data["job"]}**\n\n**裝備**\n' + ('\n'.join(equipment) or '未裝備') +
                           '\n\n**技能策略**\n' + ('\n'.join(rules) or '資料不完整') +
                           f'\n普通攻擊目標：{TARGETS.get(data.get("basic_target", "lowest"), "無效目標")}' +
                           f'\n\n**職業被動**\n{passive_line}')
        embed = discord.Embed(title=f'出戰配置｜{profile["name"]}', description=description, color=0x8B5CF6)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='配置不包含料理與藥水；遺失的裝備會略過並留空，其他套用錯誤不會改動目前配置。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /冒險 → 出戰配置 開啟自己的面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('面板已關閉，請重新使用 /冒險 → 出戰配置。', ephemeral=True)
                return
            if action == 'home':
                await navigate(self, interaction)
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='出戰配置面板已關閉。', embed=None, view=None)
                return
            if action == 'rename':
                await interaction.response.send_modal(RenameLoadoutModal(self))
                return
            notice = None
            try:
                if action == 'slot':
                    if value not in ('1', '2', '3'):
                        raise CharacterError('無效的出戰配置格。')
                    self.slot = int(value)
                elif action == 'save':
                    profile = self.cog.loadouts.save(self.guild_id, self.owner.id, self.slot)
                    notice = f'已將目前的職業、裝備與技能保存至「{profile["name"]}」。'
                elif action == 'apply':
                    profile = self.current()
                    state = self.cog.loadouts.apply(self.guild_id, self.owner.id, self.slot)
                    notice = f'已套用「{profile["name"]}」。'
                    missing_slots = [slot for slot in profile['data']['equipment']
                                     if slot not in state['equipped_instances']]
                    if missing_slots:
                        notice += f'已略過遺失的裝備：{"、".join(missing_slots)}，對應欄位留空。'
                elif action == 'rename_value':
                    profile = self.cog.loadouts.rename(self.guild_id, self.owner.id, self.slot, value)
                    notice = f'配置已重新命名為「{profile["name"]}」。'
                elif action == 'clear':
                    name = self.current()['name']
                    self.cog.loadouts.clear(self.guild_id, self.owner.id, self.slot)
                    notice = f'已清空「{name}」。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='出戰配置面板已逾時，請重新使用 /冒險。', view=None)
            except discord.HTTPException:
                pass
