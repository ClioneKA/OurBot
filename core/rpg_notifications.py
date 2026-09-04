"""Opt-in, permissionless raid notification roles, one per guild."""
import asyncio
import discord

from core.rpg_character import CharacterError


class RaidNotifications:
    def __init__(self, store):
        self.db = store.db
        self.locks = {}
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_notification_roles (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)')

    async def ensure(self, guild, create=True):
        async with self.locks.setdefault(guild.id, asyncio.Lock()):
            row = self.db.execute('SELECT role_id FROM rpg_notification_roles WHERE guild_id=?', (guild.id,)).fetchone()
            role = guild.get_role(row[0]) if row else None
            if row and role is None:
                role = next((r for r in await guild.fetch_roles() if r.id == row[0]), None)
            if role is None:
                if not create:
                    return None
                if not guild.me or not guild.me.guild_permissions.manage_roles:
                    raise CharacterError('安安需要「管理身分組」權限，才能建立討伐通知身分組。')
                role = await guild.create_role(name='安安大冒險・討伐通知', permissions=discord.Permissions.none(),
                                               mentionable=True, reason='建立玩家自行訂閱的討伐通知身分組')
                with self.db:
                    self.db.execute('INSERT INTO rpg_notification_roles VALUES (?,?) '
                                    'ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id', (guild.id, role.id))
            if role.managed or role.is_default() or role.permissions.value != 0:
                raise CharacterError('討伐通知身分組必須是無額外權限的一般身分組，請管理員檢查。')
            if not role.mentionable:
                role = await role.edit(mentionable=True, reason='允許討伐活動標記訂閱者')
            return role

    async def subscribe(self, guild, member, enabled=True):
        role = await self.ensure(guild, create=enabled)
        if role is None:
            return '你尚未訂閱討伐通知。'
        if not guild.me.guild_permissions.manage_roles or not role.is_assignable():
            raise CharacterError('安安需要「管理身分組」權限，且安安的最高身分組必須高於討伐通知身分組。')
        if enabled:
            await member.add_roles(role, reason='玩家領取討伐通知', atomic=True)
            return '已領取討伐通知身分組！新討伐出現時會標記你。'
        await member.remove_roles(role, reason='玩家取消討伐通知', atomic=True)
        return '已取消討伐通知。'
