"""Unified entry panel for player-triggered battle modes."""
import asyncio

import discord

from core.rpg import level_for
from core.rpg_character import CharacterError
from core.rpg_total_battle import TotalRaidError
from core.rpg_total_raids import WITCH_BOSS
from core.rpg_witch_rest import MAX_ENRAGE, MIN_LEVEL, WITCHES
from core.rpg_menu import add_favorite_toggle


COMMON_ENRAGES = (0, 50, 99, 100, 250, 500, 750, 1000, 2000, 4000)


class EnrageModal(discord.ui.Modal, title='輸入魔女化'):
    value = discord.ui.TextInput(label='魔女化（0～4,000%）', placeholder='例如：1000', max_length=4)

    def __init__(self, view):
        super().__init__()
        self.panel = view

    async def on_submit(self, interaction):
        try:
            value = int(str(self.value))
            if not 0 <= value <= MAX_ENRAGE:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('魔女化必須是 0～4,000 的整數。', ephemeral=True)
            return
        self.panel.enrage = value
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class RaidHubSelect(discord.ui.Select):
    def __init__(self, kind, options, placeholder, row):
        super().__init__(placeholder=placeholder, options=options, row=row)
        self.kind = kind

    async def callback(self, interaction):
        await self.view.handle(interaction, self.kind, self.values[0])


class RaidHubView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.witch_id, self.enrage = 'ema', 100
        self.practice = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def button(self, label, action, row, *, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = lambda interaction: self.handle(interaction, action)
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        self.add_item(RaidHubSelect('witch', [
            discord.SelectOption(label=witch.name, value=witch_id, default=witch_id == self.witch_id)
            for witch_id, witch in WITCHES.items()], '選擇魔女安息儀式 Boss', 0))
        self.add_item(RaidHubSelect('enrage', [
            discord.SelectOption(label=f'{value:,}%', value=str(value), default=value == self.enrage)
            for value in COMMON_ENRAGES], '選擇常用魔女化', 1))
        self.button('自訂魔女化', 'custom', 2)
        self.button(f'練習模式：{"開" if self.practice else "關"}', 'practice', 2)
        self.button('建立安息儀式', 'create_rest', 2, style=discord.ButtonStyle.danger)
        self.button('開啟魔女試煉', 'witch_trial', 3, style=discord.ButtonStyle.primary)
        self.button('繪境／特殊召喚', 'items', 3)
        self.button('返回主選單', 'back', 4)
        self.button('關閉', 'close', 4)
        add_favorite_toggle(self, 4, 'raids')

    def embed(self, notice=None):
        level = level_for(self.cog.store.xp(self.guild_id, self.owner.id))
        witch = WITCHES[self.witch_id]
        progress = self.cog.witch_rest.progress(self.guild_id, self.owner.id, self.witch_id)
        mode = '自動戰鬥（1～6 人）' if self.enrage < 100 else '手動戰鬥（3～6 人）'
        challenge = '練習模式（不收費、不發獎）' if self.practice else '正式挑戰'
        description = (f'**魔女安息儀式**\n{witch.name}・魔女化 {self.enrage:,}%｜{mode}\n'
                       f'{challenge}｜Lv.{MIN_LEVEL} 解鎖\n'
                       + ('開戰時每人消耗討伐之證 ×10\n\n' if not self.practice else '\n') +
                       '**其他可手動開啟的模式**\n'
                       '魔女試煉可直接建房；繪境迷宮與特殊召喚由背包選擇對應道具。')
        embed = discord.Embed(title='安安大冒險｜討伐', color=0xA855F7, description=description)
        record = (f'最高通關：{max(0, progress["highest_enrage"]):,}%\n'
                  f'總勝場：{progress["total_wins"]}｜有效勝場：{progress["eligible_wins"]}\n'
                  f'連續未獲秘寶：{progress["dry_wins"]} 場｜'
                  f'秘寶機率加成：+{progress["luck_points"] * .5:g}%')
        earned = [title for threshold, title in zip((1_000, 2_000, 4_000), witch.titles)
                  if progress['highest_enrage'] >= threshold]
        if earned:
            record += '\n里程碑稱號：' + '｜'.join(earned)
        embed.add_field(name='個人紀錄', value=record, inline=False)
        if level < MIN_LEVEL:
            embed.add_field(name='尚未解鎖', value=f'目前 Lv.{level}，魔女安息儀式需要 Lv.{MIN_LEVEL}。', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='0～99% 為自動戰鬥；100～4,000% 為逐回合手動戰鬥。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請使用 /討伐 開啟自己的面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        if action == 'custom':
            await interaction.response.send_modal(EnrageModal(self))
            return
        async with self.lock:
            if action == 'witch' and value in WITCHES:
                self.witch_id = value
            elif action == 'enrage' and int(value) in COMMON_ENRAGES:
                self.enrage = int(value)
            elif action == 'practice':
                self.practice = not self.practice
            elif action == 'create_rest':
                level = level_for(self.cog.store.xp(self.guild_id, self.owner.id))
                if level < MIN_LEVEL:
                    await interaction.response.edit_message(
                        embed=self.embed(f'魔女安息儀式需要 Lv.{MIN_LEVEL}。'), view=self)
                    return
                await interaction.response.defer(ephemeral=True)
                try:
                    _, channel = await self.cog.witch_rest_service.create_room(
                        interaction, self.witch_id, self.enrage, self.practice)
                    await interaction.followup.send(f'已建立房間：{channel.mention}', ephemeral=True)
                except (CharacterError, discord.HTTPException) as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                return
            elif action == 'witch_trial':
                await interaction.response.defer(ephemeral=True)
                try:
                    _, channel = await self.cog.total_raids.create_room(interaction.guild, interaction.user, WITCH_BOSS)
                    await interaction.followup.send(f'已開啟當日魔女試煉：{channel.mention}', ephemeral=True)
                except (CharacterError, TotalRaidError, discord.HTTPException) as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                return
            elif action == 'items':
                from core.rpg_menu import navigate
                await navigate(self, interaction, 'use_items')
                return
            elif action == 'back':
                from core.rpg_menu import navigate
                await navigate(self, interaction, 'home')
                return
            elif action == 'close':
                await interaction.response.edit_message(content='討伐面板已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)
