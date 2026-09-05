"""Opt-in, permissionless notification roles for regular and mid-tier raids."""
import asyncio
import discord

from core.rpg_character import CharacterError


class RaidNotifications:
    def __init__(self, store):
        self.db = store.db
        self.locks = {}
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_notification_roles (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_mid_notification_roles (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)')

    async def ensure(self, guild, kind='regular', create=True):
        table = 'rpg_mid_notification_roles' if kind == 'mid' else 'rpg_notification_roles'
        label = '中階討伐' if kind == 'mid' else '討伐'
        async with self.locks.setdefault((guild.id, kind), asyncio.Lock()):
            row = self.db.execute(f'SELECT role_id FROM {table} WHERE guild_id=?', (guild.id,)).fetchone()
            role = guild.get_role(row[0]) if row else None
            if row and role is None:
                role = next((r for r in await guild.fetch_roles() if r.id == row[0]), None)
            if role is None:
                if not create:
                    return None
                if not guild.me or not guild.me.guild_permissions.manage_roles:
                    raise CharacterError('安安需要「管理身分組」權限，才能建立討伐通知身分組。')
                role = await guild.create_role(name=f'安安大冒險・{label}通知', permissions=discord.Permissions.none(),
                                               mentionable=True, reason=f'建立玩家自行訂閱的{label}通知身分組')
                with self.db:
                    self.db.execute(f'INSERT INTO {table} VALUES (?,?) '
                                    'ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id', (guild.id, role.id))
            if role.managed or role.is_default() or role.permissions.value != 0:
                raise CharacterError('討伐通知身分組必須是無額外權限的一般身分組，請管理員檢查。')
            if not role.mentionable:
                role = await role.edit(mentionable=True, reason='允許討伐活動標記訂閱者')
            return role

    async def subscribe(self, guild, member, enabled=True, kind='regular'):
        role = await self.ensure(guild, kind, create=enabled)
        label = '中階討伐' if kind == 'mid' else '討伐'
        if role is None:
            return '你尚未訂閱討伐通知。'
        if not guild.me.guild_permissions.manage_roles or not role.is_assignable():
            raise CharacterError('安安需要「管理身分組」權限，且安安的最高身分組必須高於討伐通知身分組。')
        if enabled:
            await member.add_roles(role, reason=f'玩家領取{label}通知', atomic=True)
            return f'已領取{label}通知身分組！新討伐出現時會標記你。'
        await member.remove_roles(role, reason=f'玩家取消{label}通知', atomic=True)
        return f'已取消{label}通知。'
