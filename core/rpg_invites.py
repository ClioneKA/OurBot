"""Persistent invitations and configured adventurer-role assignment."""
import asyncio
import os
import time
import uuid

import discord

from core.rpg_character import CharacterError


def role_ids(raw):
    try:
        result = tuple(dict.fromkeys(int(part.strip()) for part in raw.split(',') if part.strip()))
        if any(value <= 0 for value in result):
            raise ValueError
        return result
    except ValueError as exc:
        raise ValueError('RPG_ADVENTURER_ROLE_IDS 必須是逗號分隔的正整數身分組 ID') from exc


class InvitationStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_invitations (
                token TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
                inviter_id INTEGER NOT NULL, target_user_id INTEGER,
                message_id INTEGER, created_at REAL NOT NULL, claimed_at REAL)''')

    def create(self, guild_id, inviter_id, target_user_id=None):
        token = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO rpg_invitations VALUES (?,?,?,?,?,?,NULL)',
                            (token, guild_id, inviter_id, target_user_id, None, time.time()))
        return token

    def bind_message(self, token, message_id):
        with self.db:
            self.db.execute('UPDATE rpg_invitations SET message_id=? WHERE token=?',
                            (message_id, token))

    def delete(self, token):
        with self.db:
            self.db.execute('DELETE FROM rpg_invitations WHERE token=?', (token,))

    def get(self, token):
        row = self.db.execute('''SELECT token,guild_id,inviter_id,target_user_id,message_id,
            created_at,claimed_at FROM rpg_invitations WHERE token=?''', (token,)).fetchone()
        if not row:
            return None
        keys = ('token', 'guild_id', 'inviter_id', 'target_user_id', 'message_id',
                'created_at', 'claimed_at')
        return dict(zip(keys, row))

    def active(self):
        rows = self.db.execute('''SELECT token,guild_id,inviter_id,target_user_id,message_id,
            created_at,claimed_at FROM rpg_invitations
            WHERE target_user_id IS NULL OR claimed_at IS NULL''').fetchall()
        keys = ('token', 'guild_id', 'inviter_id', 'target_user_id', 'message_id',
                'created_at', 'claimed_at')
        return [dict(zip(keys, row)) for row in rows]

    def claim(self, token, user_id):
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                return self.claim(token, user_id)
        changed = self.db.execute('''UPDATE rpg_invitations SET claimed_at=?
            WHERE token=? AND target_user_id=? AND claimed_at IS NULL''',
                                  (time.time(), token, user_id))
        return bool(changed.rowcount)


class AdventurerInvitationView(discord.ui.View):
    def __init__(self, service, token):
        super().__init__(timeout=None)
        self.service, self.token = service, token
        self.accept_button.custom_id = f'rpg:invitation:accept:{token}'

    @discord.ui.button(label='接受邀請', style=discord.ButtonStyle.success,
                       custom_id='rpg:invitation:accept')
    async def accept_button(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.service.accept(self.token, interaction.user)
        except CharacterError as exc:
            message = str(exc)
        await interaction.followup.send(message, ephemeral=True)


class AdventurerInvitations:
    def __init__(self, cog):
        self.cog, self.bot = cog, cog.bot
        self.repo = InvitationStore(cog.store)
        self.configured_role_ids = role_ids(os.getenv('RPG_ADVENTURER_ROLE_IDS', ''))
        self.locks = {}

    def restore_views(self):
        for invitation in self.repo.active():
            self.bot.add_view(AdventurerInvitationView(self, invitation['token']),
                              message_id=invitation['message_id'])

    async def role_for(self, guild):
        space = self.cog.spaces.store.get(guild.id) if hasattr(self.cog, 'spaces') else None
        candidate_ids = ((space.adventurer_role_id,) if space and space.adventurer_role_id else ()) + self.configured_role_ids
        if not candidate_ids:
            raise CharacterError('尚未設定冒險者身分組，請管理員使用 /冒險區域 建立，或設定 RPG_ADVENTURER_ROLE_IDS。')
        role = next((guild.get_role(role_id) for role_id in candidate_ids
                     if guild.get_role(role_id) is not None), None)
        if role is None:
            try:
                fetched = await guild.fetch_roles()
            except discord.HTTPException as exc:
                raise CharacterError('無法讀取已設定的冒險者身分組，請稍後再試。') from exc
            role = next((item for item in fetched if item.id in candidate_ids), None)
        if role is None:
            raise CharacterError('這個伺服器尚未配置冒險者身分組。')
        if role.managed or role.is_default():
            raise CharacterError('冒險者身分組不能是整合管理或 @everyone 身分組。')
        if not guild.me or not guild.me.guild_permissions.manage_roles or not role.is_assignable():
            raise CharacterError('安安需要「管理身分組」權限，且最高身分組必須高於冒險者身分組。')
        return role

    async def accept(self, token, user):
        async with self.locks.setdefault(token, asyncio.Lock()):
            return await self._accept(token, user)

    async def _accept(self, token, user):
        invitation = self.repo.get(token)
        if not invitation:
            raise CharacterError('這封邀請函已失效。')
        target = invitation['target_user_id']
        if target is not None and target != user.id:
            raise CharacterError('這封邀請函是寄給其他人的。')
        if target is not None and invitation['claimed_at'] is not None:
            raise CharacterError('這封邀請函已經使用過了。')
        guild = self.bot.get_guild(invitation['guild_id'])
        if guild is None:
            raise CharacterError('找不到邀請函所屬的伺服器。')
        try:
            member = guild.get_member(user.id) or await guild.fetch_member(user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise CharacterError('你必須仍在邀請函所屬的伺服器中。') from exc
        if member.bot:
            raise CharacterError('機器人不能成為冒險者。')
        role = await self.role_for(guild)
        had_role = role in getattr(member, 'roles', ())
        if not had_role:
            try:
                await member.add_roles(role, reason='接受安安大冒險邀請', atomic=True)
            except discord.Forbidden as exc:
                raise CharacterError('無法授予冒險者身分組，請管理員檢查身分組權限與位置。') from exc
            except discord.HTTPException as exc:
                raise CharacterError('Discord 暫時無法授予冒險者身分組，請稍後再試。') from exc
        try:
            with self.cog.store.db:
                self.cog.store.db.execute('BEGIN IMMEDIATE')
                created = self.cog.characters.create(guild.id, member.id)
                if target is not None and not self.repo.claim(token, member.id):
                    raise CharacterError('這封邀請函已經使用過了。')
        except Exception:
            if not had_role:
                try:
                    await member.remove_roles(role, reason='建立冒險角色失敗，回復身分組', atomic=True)
                except discord.HTTPException:
                    pass
            raise
        return ('邀請接受成功！角色已建立，初始木棒也已裝備。使用 `/冒險` 開始旅程吧！'
                if created else '你已經是冒險者；冒險者身分組已確認。')
