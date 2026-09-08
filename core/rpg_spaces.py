"""Managed Discord category, channels and access role for the RPG."""
from dataclasses import dataclass

import discord

from core.rpg_character import CharacterError


SPACE_CHANNELS = {
    'regular_channel_id': ('一般討伐', '一般討伐'),
    'mid_channel_id': ('中階討伐', '中階討伐'),
    'high_channel_id': ('高階討伐', '高階討伐'),
    'tavern_channel_id': ('冒險者酒館', '冒險者酒館'),
}


@dataclass(frozen=True)
class AdventureSpace:
    guild_id: int
    category_id: int | None = None
    regular_channel_id: int | None = None
    mid_channel_id: int | None = None
    high_channel_id: int | None = None
    tavern_channel_id: int | None = None
    adventurer_role_id: int | None = None


class AdventureSpaceStore:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_adventure_spaces (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                regular_channel_id INTEGER,
                mid_channel_id INTEGER,
                high_channel_id INTEGER,
                tavern_channel_id INTEGER,
                adventurer_role_id INTEGER)''')

    @staticmethod
    def _from_row(row):
        return AdventureSpace(*row) if row else None

    def get(self, guild_id):
        return self._from_row(self.db.execute('''SELECT guild_id,category_id,regular_channel_id,
            mid_channel_id,high_channel_id,tavern_channel_id,adventurer_role_id
            FROM rpg_adventure_spaces WHERE guild_id=?''', (guild_id,)).fetchone())

    def all(self):
        return [self._from_row(row) for row in self.db.execute('''SELECT guild_id,category_id,
            regular_channel_id,mid_channel_id,high_channel_id,tavern_channel_id,adventurer_role_id
            FROM rpg_adventure_spaces''').fetchall()]

    def save(self, space):
        with self.db:
            self.db.execute('''INSERT INTO rpg_adventure_spaces VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                category_id=excluded.category_id,
                regular_channel_id=excluded.regular_channel_id,
                mid_channel_id=excluded.mid_channel_id,
                high_channel_id=excluded.high_channel_id,
                tavern_channel_id=excluded.tavern_channel_id,
                adventurer_role_id=excluded.adventurer_role_id''', (
                    space.guild_id, space.category_id, space.regular_channel_id,
                    space.mid_channel_id, space.high_channel_id, space.tavern_channel_id,
                    space.adventurer_role_id))
        return space

    def player_ids(self, guild_id):
        return [row[0] for row in self.db.execute(
            'SELECT user_id FROM players WHERE guild_id=? ORDER BY user_id', (guild_id,)).fetchall()]


class AdventureSpaceService:
    def __init__(self, cog):
        self.cog = cog
        self.store = AdventureSpaceStore(cog.store)

    @staticmethod
    def _valid_role(guild, role_id):
        role = guild.get_role(role_id) if role_id else None
        return role if role is not None and not role.managed and not role.is_default() else None

    @staticmethod
    def _valid_channel(guild, channel_id):
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    @staticmethod
    def _valid_category(guild, category_id):
        category = guild.get_channel(category_id) if category_id else None
        return category if isinstance(category, discord.CategoryChannel) else None

    def _environment_candidates(self, guild):
        raids = self.cog.raids
        channel_sets = {
            'regular_channel_id': raids.environment_channels,
            'mid_channel_id': raids.environment_mid_channels,
            'high_channel_id': raids.environment_high_channels,
            'tavern_channel_id': self.cog.tavern.environment_channel_ids,
        }
        result = {}
        for field, configured in channel_sets.items():
            result[field] = next((channel.id for channel_id in sorted(configured)
                                  if (channel := self._valid_channel(guild, channel_id)) is not None), None)
        result['adventurer_role_id'] = next((role.id for role_id in self.cog.invitations.configured_role_ids
                                             if (role := self._valid_role(guild, role_id)) is not None), None)
        return result

    def inspect(self, guild):
        space = self.store.get(guild.id)
        objects = {
            'category': self._valid_category(guild, space.category_id) if space else None,
            'role': self._valid_role(guild, space.adventurer_role_id) if space else None,
        }
        for field in SPACE_CHANNELS:
            objects[field] = self._valid_channel(guild, getattr(space, field)) if space else None
        complete = all(objects.values())
        aligned = bool(objects['category']) and all(
            channel and channel.category_id == objects['category'].id
            for channel in (objects[field] for field in SPACE_CHANNELS))
        return space, objects, complete, aligned

    def status_text(self, guild):
        space, objects, complete, aligned = self.inspect(guild)
        lines = []
        if space is None:
            lines.append('尚未登記；執行「建立／匯入」會優先沿用 `.env` 的有效配置。')
        category = objects['category']
        role = objects['role']
        lines.append(f'分類：{category.mention if category else "缺少"}')
        lines.append(f'冒險者身分組：{role.mention if role else "缺少"}')
        for field, (label, _name) in SPACE_CHANNELS.items():
            channel = objects[field]
            placement = '' if not channel or not category or channel.category_id == category.id else '（不在冒險分類）'
            lines.append(f'{label}：{channel.mention if channel else "缺少"}{placement}')
        if complete:
            permission_issues = self._permission_issues(guild, objects)
            if not aligned or permission_issues:
                reasons = []
                if not aligned:
                    reasons.append('頻道未統一放在冒險分類')
                if permission_issues:
                    reasons.append('必要權限尚未一致')
                lines.append(f'狀態：項目完整，但{"、".join(reasons)}；可執行「修復」。')
            else:
                lines.append('狀態：配置、分類與必要權限皆正常。')
        return '\n'.join(lines)

    @staticmethod
    def _permission_issues(guild, objects):
        category, role = objects['category'], objects['role']
        if not category or not role or not guild.me:
            return ['缺少分類、身分組或安安成員資料']
        checks = [
            (category, guild.default_role, 'view_channel', False),
            (category, role, 'view_channel', True),
        ]
        for field in SPACE_CHANNELS:
            channel = objects[field]
            if not channel:
                continue
            tavern = field == 'tavern_channel_id'
            checks.extend(((channel, guild.default_role, 'view_channel', False),
                           (channel, role, 'view_channel', True),
                           (channel, role, 'send_messages', tavern),
                           (channel, guild.me, 'send_messages', True)))
        issues = []
        for subject, target, permission, expected in checks:
            overwrite = subject.overwrites_for(target)
            if getattr(overwrite, permission) is not expected:
                issues.append(permission)
        return issues

    def _check_permissions(self, guild):
        permissions = guild.me.guild_permissions if guild.me else None
        if not permissions or not permissions.manage_channels or not permissions.manage_roles:
            raise CharacterError('安安需要「管理頻道」與「管理身分組」權限。')

    async def _sync_adventurers(self, guild, role):
        added = failed = 0
        getter = getattr(guild, 'get_member', None)
        for user_id in self.store.player_ids(guild.id):
            member = getter(user_id) if callable(getter) else None
            if member is None or member.bot or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason='同步既有安安大冒險玩家身分組', atomic=True)
                added += 1
            except discord.HTTPException:
                failed += 1
        return added, failed

    async def setup(self, guild):
        """Adopt valid configured objects and create only missing objects."""
        self._check_permissions(guild)
        current = self.store.get(guild.id) or AdventureSpace(guild.id)
        imported = self._environment_candidates(guild)
        values = {field: getattr(current, field) for field in SPACE_CHANNELS}
        role = self._valid_role(guild, current.adventurer_role_id)
        for field in SPACE_CHANNELS:
            if self._valid_channel(guild, values[field]) is None:
                values[field] = imported[field]
        if role is None:
            role = self._valid_role(guild, imported['adventurer_role_id'])
        if role is None:
            role = await guild.create_role(name='安安大冒險・冒險者', permissions=discord.Permissions.none(),
                                           mentionable=False, reason='建立安安大冒險存取身分組')
        if callable(getattr(role, 'is_assignable', None)) and not role.is_assignable():
            raise CharacterError('安安的最高身分組必須高於冒險者身分組，才能自動指派成員。')
        self.store.save(AdventureSpace(
            guild.id, current.category_id, values['regular_channel_id'], values['mid_channel_id'],
            values['high_channel_id'], values['tavern_channel_id'], role.id))

        category = self._valid_category(guild, current.category_id)
        existing_channels = [self._valid_channel(guild, values[field]) for field in SPACE_CHANNELS]
        existing_categories = {channel.category_id for channel in existing_channels
                               if channel is not None and channel.category_id is not None}
        if category is None and len(existing_categories) == 1:
            category = self._valid_category(guild, next(iter(existing_categories)))
        if category is None:
            category = await guild.create_category(
                '安安大冒險', overwrites=self._category_overwrites(guild, role),
                reason='建立安安大冒險分類')

        def save_progress():
            return self.store.save(AdventureSpace(
                guild.id, category.id, values['regular_channel_id'], values['mid_channel_id'],
                values['high_channel_id'], values['tavern_channel_id'], role.id))

        # Persist each Discord object before creating the next one, so a failed
        # Discord request can be retried without duplicating earlier creations.
        save_progress()

        created = []
        for field, (label, name) in SPACE_CHANNELS.items():
            if self._valid_channel(guild, values[field]) is not None:
                continue
            overwrites = self._channel_overwrites(guild, role, tavern=field == 'tavern_channel_id')
            channel = await guild.create_text_channel(name, category=category, overwrites=overwrites,
                                                       reason=f'建立{label}頻道')
            values[field] = channel.id
            created.append(label)
            save_progress()
        self.cog.raids.refresh_channels()
        for kind in ('regular', 'mid', 'high'):
            await self.cog.raids.notifications.ensure(guild, kind)
        role_added, role_failed = await self._sync_adventurers(guild, role)
        detail = '、'.join(created) if created else '沒有建立新頻道'
        role_detail = f'；已替 {role_added} 名既有玩家補上冒險者身分組' if role_added else ''
        if role_failed:
            role_detail += f'；另有 {role_failed} 名玩家同步失敗'
        return (f'已登記冒險區域；{detail}。既有頻道尚未移動或改權限{role_detail}。'
                '可用「查看狀態」確認 Discord 更新結果。')

    @staticmethod
    def _category_overwrites(guild, role):
        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                               use_application_commands=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=True,
                embed_links=True, attach_files=True, manage_messages=True),
        }

    @staticmethod
    def _channel_overwrites(guild, role, *, tavern):
        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, use_application_commands=True,
                send_messages=tavern, embed_links=tavern, attach_files=tavern,
                add_reactions=tavern, create_public_threads=tavern,
                create_private_threads=False, send_messages_in_threads=tavern),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=True,
                embed_links=True, attach_files=True, manage_messages=True),
        }

    async def repair(self, guild):
        """Create missing objects, then align category placement and required overwrites."""
        await self.setup(guild)
        space, objects, _complete, _aligned = self.inspect(guild)
        category, role = objects['category'], objects['role']
        await category.set_permissions(guild.default_role, view_channel=False,
                                       reason='限制冒險區域僅冒險者可見')
        await category.set_permissions(role, view_channel=True, read_message_history=True,
                                       use_application_commands=True, reason='開放冒險者查看冒險區域')
        await category.set_permissions(guild.me, view_channel=True, read_message_history=True,
                                       send_messages=True, embed_links=True, attach_files=True,
                                       manage_messages=True, reason='允許安安管理冒險區域')
        for field, (label, _name) in SPACE_CHANNELS.items():
            channel = objects[field]
            if channel.category_id != category.id:
                await channel.edit(category=category, sync_permissions=False,
                                   reason=f'將{label}移入冒險分類')
            overwrites = self._channel_overwrites(guild, role, tavern=field == 'tavern_channel_id')
            for target, overwrite in overwrites.items():
                await channel.set_permissions(target, overwrite=overwrite,
                                              reason=f'修復{label}必要權限')
        return ('已統一冒險區域的分類與必要權限；未刪除頻道、身分組或額外權限覆寫。'
                '可用「查看狀態」確認 Discord 更新結果。')
