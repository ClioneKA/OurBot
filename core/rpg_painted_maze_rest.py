"""Room-local skill editing with the ordinary skill panel and frozen character state."""
from dataclasses import asdict
import sqlite3
from types import SimpleNamespace

import discord

from core.rpg import level_floor
from core.rpg_battle import Tactics, condition_text, rule_skill, skill_description
from core.rpg_character import CharacterError
from core.rpg_skill_view import SkillView


class RestTactics:
    """Reuse normal skill validation without writing the player's global loadout."""
    def __init__(self, repo, room_id, user_id, index):
        self.repo, self.room_id, self.user_id, self.index = repo, room_id, user_id, index
        self.db = sqlite3.connect(':memory:')
        self.level = 50
        self.tactics = Tactics(SimpleNamespace(db=self.db, xp=lambda *_: level_floor(self.level)))
        self.synchronize()

    def synchronize(self):
        room, player = self.repo.rest_participant(self.room_id, self.user_id, expected_index=self.index)
        self.guild_id, self.job, self.level = room['guild_id'], player['state']['job'], player['state']['level']
        with self.db:
            self.db.execute('DELETE FROM rpg_tactics')
            self.db.execute('DELETE FROM rpg_passives')
            for rule in player['rules']:
                self.db.execute('INSERT INTO rpg_tactics VALUES (?,?,?,?,?,?,?,?,?,?)', (
                    self.guild_id, self.user_id, self.job, rule['slot'], rule['priority'], int(rule['enabled']),
                    rule['condition'], rule['target'], rule.get('skill_id'), rule.get('condition_value')))
            if player.get('passive_id') is not None:
                self.db.execute('INSERT INTO rpg_passives VALUES (?,?,?,?)',
                                (self.guild_id, self.user_id, self.job, player['passive_id']))

    def __getattr__(self, name):
        return getattr(self.tactics, name)

    def _change(self, method, *args):
        result = getattr(self.tactics, method)(*args)
        passive = self.tactics.passive(self.guild_id, self.user_id, self.job)
        self.repo.save_rest_tactics(self.room_id, self.user_id,
            [asdict(rule) for rule in self.tactics.rules(self.guild_id, self.user_id, self.job)],
            passive.id if passive else None, expected_index=self.index)
        return result

    def equip(self, *args):
        return self._change('equip', *args)

    def equip_passive(self, *args):
        return self._change('equip_passive', *args)

    def configure(self, *args):
        return self._change('configure', *args)


class MazeSkillView(SkillView):
    def __init__(self, service, room, interaction):
        self.service, self.room_id, self.index = service, room['id'], room['boss_index']
        self.rest_tactics = RestTactics(service.repo, room['id'], interaction.user.id, self.index)
        proxy = SimpleNamespace(tactics=self.rest_tactics,
            characters=SimpleNamespace(job=lambda *_: self.rest_tactics.job), skills_embed=self.skills_embed)
        super().__init__(proxy, interaction)

    def skills_embed(self, guild_id, user_id):
        embed = discord.Embed(title=f'繪境迷廊｜休息點技能設定｜{self.rest_tactics.job}', color=0x7C3AED)
        for rule in sorted(self.rest_tactics.rules(guild_id, user_id, self.rest_tactics.job), key=lambda r: r.slot):
            skill = rule_skill(self.rest_tactics.job, rule)
            embed.add_field(name=f'槽 {rule.slot}｜{skill.name}｜優先 {rule.priority}',
                value=f'{skill_description(skill)}\n{condition_text(rule.condition, rule.condition_value)}', inline=False)
        embed.description = '只調整本房間的技能；職業、裝備與攜帶效果維持入場時的狀態。修改後需重新確認準備。'
        return embed

    def rebuild(self):
        super().rebuild()
        for item in list(self.children):
            if getattr(item, 'label', None) == '返回主選單':
                self.remove_item(item)

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        if action == 'home':
            await interaction.response.send_message('休息點只能調整技能。', ephemeral=True)
            return
        async with self.service.lock(self.room_id):
            try:
                if self.closed or self.is_finished():
                    await interaction.response.send_message('技能面板已關閉，請從休息點重新開啟。', ephemeral=True)
                    return
                self.rest_tactics.synchronize()
                await super().handle(interaction, action, value)
                await self.service._refresh(self.service.repo.get(self.room_id))
            except CharacterError as exc:
                if interaction.response.is_done():
                    await interaction.followup.send(str(exc), ephemeral=True)
                else:
                    await interaction.response.send_message(str(exc), ephemeral=True)

    def stop(self):
        super().stop()
        self.rest_tactics.db.close()

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='技能面板已逾時，請從休息點重新開啟。', view=None)
            except discord.HTTPException:
                pass
