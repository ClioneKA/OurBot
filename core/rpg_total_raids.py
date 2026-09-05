"""Discord room lifecycle and interaction views for manual total raids."""
import asyncio
from dataclasses import asdict
import json
import logging
import os
import random
import time
import uuid

import discord
from discord.ext import tasks

from core.rpg_battle import rule_skill
from core.rpg_character import CharacterError
from core.rpg_raids import channel_ids
from core.rpg_total_battle import (
    ACTION_ATTACK,
    ACTION_SKILL,
    TotalRaidError,
    dump_total_battle,
    load_total_battle,
    training_dummy_battle_from_participants,
)


logger = logging.getLogger(__name__)
TOTAL_RAID_BOSSES = ('訓練用假人',)


class TotalRaidStore:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_total_raids (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_total_raid_numbers (
                guild_id INTEGER NOT NULL, boss TEXT NOT NULL, next_number INTEGER NOT NULL,
                PRIMARY KEY (guild_id, boss))''')

    def reserve_number(self, guild_id, boss):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT next_number FROM rpg_total_raid_numbers WHERE guild_id=? AND boss=?',
                (guild_id, boss),
            ).fetchone()
            number = row[0] if row else 1
            self.db.execute('''INSERT INTO rpg_total_raid_numbers VALUES (?,?,?)
                ON CONFLICT(guild_id,boss) DO UPDATE SET next_number=excluded.next_number''',
                (guild_id, boss, number + 1),
            )
            return number

    def create(self, guild_id, category_id, channel_id, host_id, boss, number):
        room = dict(
            id=uuid.uuid4().hex,
            guild_id=guild_id,
            category_id=category_id,
            channel_id=channel_id,
            message_id=None,
            host_id=host_id,
            boss=boss,
            number=number,
            status='lobby',
            members=[host_id],
            participants=[],
            battle=None,
            round_deadline=None,
            created_at=time.time(),
        )
        with self.db:
            self.db.execute(
                'INSERT INTO rpg_total_raids VALUES (?,?,?,?,?,?)',
                (room['id'], guild_id, category_id, channel_id, room['status'],
                 json.dumps(room, ensure_ascii=False)),
            )
        return room

    def get(self, room_id):
        row = self.db.execute('SELECT data FROM rpg_total_raids WHERE id=?', (room_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM rpg_total_raids WHERE status IN ('lobby','running')")]

    def save(self, room):
        with self.db:
            self.db.execute('UPDATE rpg_total_raids SET status=?, data=? WHERE id=?',
                            (room['status'], json.dumps(room, ensure_ascii=False), room['id']))


class TotalRaidLobbyView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    async def _change(self, interaction, leave=False):
        try:
            room = await self.service.change_member(self.room_id, interaction.user, leave)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.service.lobby_embed(room), view=self)

    @discord.ui.button(label='加入隊伍', style=discord.ButtonStyle.success,
                       custom_id='total_raid:lobby:join')
    async def join(self, interaction, _button):
        await self._change(interaction)

    @discord.ui.button(label='退出隊伍', style=discord.ButtonStyle.secondary,
                       custom_id='total_raid:lobby:leave')
    async def leave(self, interaction, _button):
        await self._change(interaction, True)

    @discord.ui.button(label='開始戰鬥', style=discord.ButtonStyle.danger,
                       custom_id='total_raid:lobby:start')
    async def begin(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.service.begin(self.room_id, interaction.user)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.stop()
        await interaction.followup.send(
            f'總力戰已開始，共 {len(room["members"])} 人參戰。', ephemeral=True)

    @discord.ui.button(label='關閉房間', style=discord.ButtonStyle.secondary,
                       custom_id='total_raid:lobby:cancel')
    async def cancel(self, interaction, _button):
        try:
            await self.service.cancel_lobby(self.room_id, interaction.user)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f'總力戰房間已由 <@{interaction.user.id}> 關閉。', embed=None, view=None,
            allowed_mentions=discord.AllowedMentions.none())


class TotalRaidRunningView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    @discord.ui.button(label='選擇／修改本回合行動', style=discord.ButtonStyle.primary,
                       custom_id='total_raid:running:action')
    async def choose(self, interaction, _button):
        try:
            room, battle = self.service.running_battle(self.room_id, interaction.user.id)
            view = TotalRaidActionChoiceView(self.service, room['id'], interaction.user.id, battle)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f'第 {battle.planning_round} 回合：請選擇行動。你可以在結算前重新選擇。',
            view=view, ephemeral=True,
        )


class TotalRaidActionSelect(discord.ui.Select):
    def __init__(self, parent, battle):
        options = []
        for action in battle.available_actions(parent.user_id):
            remaining = action.get('cooldown_remaining', 0)
            if remaining:
                continue
            value = ACTION_ATTACK if action['action'] == ACTION_ATTACK else f'{ACTION_SKILL}:{action["skill_slot"]}'
            options.append(discord.SelectOption(
                label=action['name'][:100], value=value,
                description=action['description'][:100],
            ))
        super().__init__(placeholder='選擇普通攻擊或技能', options=options,
                         custom_id='total_raid:private:action')
        self.parent_view = parent

    async def callback(self, interaction):
        raw = self.values[0]
        action, slot = (raw.split(':', 1) + [None])[:2] if ':' in raw else (raw, None)
        slot = int(slot) if slot is not None else None
        try:
            _room, battle = self.parent_view.service.running_battle(
                self.parent_view.room_id, interaction.user.id)
            targets = battle.valid_targets(interaction.user.id, action, slot)
            if not targets:
                await interaction.response.defer()
                await self.parent_view.service.submit_action(
                    self.parent_view.room_id, interaction.user.id, action, None, slot)
                await interaction.edit_original_response(
                    content='本回合行動已登記；結算前仍可重新選擇。', view=None)
                return
            view = TotalRaidTargetView(
                self.parent_view.service, self.parent_view.room_id,
                interaction.user.id, action, slot, battle, targets,
            )
        except (CharacterError, TotalRaidError) as exc:
            await interaction.response.edit_message(content=str(exc), view=None)
            return
        await interaction.response.edit_message(content='請選擇目標。', view=view)


class TotalRaidActionChoiceView(discord.ui.View):
    def __init__(self, service, room_id, user_id, battle):
        super().__init__(timeout=120)
        self.service, self.room_id, self.user_id = service, room_id, user_id
        self.add_item(TotalRaidActionSelect(self, battle))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message('這不是你的行動面板。', ephemeral=True)
        return False


class TotalRaidTargetSelect(discord.ui.Select):
    def __init__(self, parent, battle, targets):
        options = []
        for key in targets:
            fighter = battle.fighter_for_key(key)
            options.append(discord.SelectOption(
                label=fighter.name[:100], value=key,
                description=f'HP {fighter.hp:,}/{fighter.stats["HP"]:,}'[:100],
            ))
        super().__init__(placeholder='選擇行動目標', options=options,
                         custom_id='total_raid:private:target')
        self.parent_view = parent

    async def callback(self, interaction):
        await interaction.response.defer()
        try:
            await self.parent_view.service.submit_action(
                self.parent_view.room_id, interaction.user.id,
                self.parent_view.action, self.values[0], self.parent_view.skill_slot,
            )
        except (CharacterError, TotalRaidError) as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        await interaction.edit_original_response(
            content='本回合行動已登記；結算前仍可重新選擇。', view=None)


class TotalRaidTargetView(discord.ui.View):
    def __init__(self, service, room_id, user_id, action, skill_slot, battle, targets):
        super().__init__(timeout=120)
        self.service, self.room_id, self.user_id = service, room_id, user_id
        self.action, self.skill_slot = action, skill_slot
        self.add_item(TotalRaidTargetSelect(self, battle, targets))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message('這不是你的目標面板。', ephemeral=True)
        return False


class TotalRaidService:
    def __init__(self, cog):
        self.cog, self.bot = cog, cog.bot
        self.settings = cog.settings.total_raid
        self.category_ids = channel_ids(
            os.getenv('RPG_TOTAL_RAID_CATEGORY_IDS', ''), 'RPG_TOTAL_RAID_CATEGORY_IDS')
        self.repo = TotalRaidStore(cog.store)
        self.locks = {}
        self.views = {}

    def start(self):
        for room in self.repo.active():
            if not room.get('message_id'):
                continue
            view = self.view(room)
            self.bot.add_view(view, message_id=room['message_id'])
        self.tick.start()

    async def close(self):
        self.tick.cancel()
        task = self.tick.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for view in self.views.values():
            view.stop()

    def view(self, room):
        key = (room['id'], room['status'])
        if key not in self.views:
            cls = TotalRaidLobbyView if room['status'] == 'lobby' else TotalRaidRunningView
            self.views[key] = cls(self, room['id'])
        return self.views[key]

    def lock(self, room_id):
        return self.locks.setdefault(room_id, asyncio.Lock())

    def category_for(self, guild):
        for category_id in sorted(self.category_ids):
            category = guild.get_channel(category_id)
            if isinstance(category, discord.CategoryChannel):
                return category
        raise CharacterError('這個伺服器尚未設定總力戰類別。')

    async def create_room(self, guild, host, boss):
        if not self.settings.enabled:
            raise CharacterError('總力戰目前未開放。')
        if boss not in TOTAL_RAID_BOSSES:
            raise CharacterError('尚未支援這個總力戰 Boss。')
        if any(active['guild_id'] == guild.id and host.id in active['members']
               for active in self.repo.active()):
            raise CharacterError('你已在這個伺服器的另一個總力戰房間中。')
        category = self.category_for(guild)
        number = self.repo.reserve_number(guild.id, boss)
        name = f'總力戰-{boss}-{number}'
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True, embed_links=True),
            host: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True),
        }
        channel = await category.create_text_channel(
            name=name, overwrites=overwrites,
            topic=f'總力戰測試房｜房主：{host}｜Boss：{boss}',
            reason=f'{host} 建立總力戰測試房',
        )
        room = self.repo.create(guild.id, category.id, channel.id, host.id, boss, number)
        try:
            message = await channel.send(embed=self.lobby_embed(room), view=self.view(room),
                                         allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            room['status'] = 'cancelled'
            self.repo.save(room)
            raise
        room['message_id'] = message.id
        self.repo.save(room)
        return room, channel

    async def change_member(self, room_id, member, leave=False):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or room['status'] != 'lobby':
                raise TotalRaidError('房間已經開始或關閉。')
            if member.bot:
                raise TotalRaidError('機器人不能參加總力戰。')
            if leave:
                if member.id == room['host_id']:
                    raise TotalRaidError('房主不能退出自己的隊伍。')
                if member.id not in room['members']:
                    raise TotalRaidError('你尚未加入這個隊伍。')
                room['members'].remove(member.id)
            else:
                if member.id in room['members']:
                    raise TotalRaidError('你已經在隊伍中。')
                if len(room['members']) >= self.settings.max_participants:
                    raise TotalRaidError('總力戰隊伍已滿。')
                for active in self.repo.active():
                    if (active['id'] != room_id and active['guild_id'] == room['guild_id']
                            and member.id in active['members']):
                        raise TotalRaidError('你已在另一個總力戰房間中。')
                room['members'].append(member.id)
            self.repo.save(room)
            return room

    async def begin(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or room['status'] != 'lobby':
                raise TotalRaidError('這個房間已經開始或關閉。')
            if member.id != room['host_id']:
                raise TotalRaidError('只有開房的房主可以開始總力戰。')
            channel = self.bot.get_channel(room['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                raise TotalRaidError('找不到總力戰文字頻道。')
            participants = []
            for user_id in room['members']:
                participant = channel.guild.get_member(user_id)
                if participant is None or participant.bot:
                    continue
                state = self.cog.characters.snapshot(room['guild_id'], user_id)
                participants.append(dict(
                    id=user_id, name=participant.display_name[:16], state=state,
                    rules=[asdict(rule) for rule in self.cog.tactics.rules(
                        room['guild_id'], user_id, state['job'])],
                ))
            if not participants:
                raise TotalRaidError('隊伍中沒有可參戰的玩家。')
            provisions = getattr(self.cog, 'provisions', None)
            if provisions is not None:
                prepared = provisions.prepare_for_raid(
                    room['id'], room['guild_id'], [item['id'] for item in participants])
                for participant in participants:
                    participant['provisions'] = prepared.get(participant['id'], {})
            seed = random.randrange(2**31)
            battle = training_dummy_battle_from_participants(
                participants, seed=seed, max_rounds=self.settings.max_rounds)
            room.update(
                status='running', members=[item['id'] for item in participants],
                participants=participants, seed=seed, battle=dump_total_battle(battle),
                round_deadline=time.time() + self.settings.action_timeout_seconds,
            )
            self.repo.save(room)
            old = self.views.pop((room_id, 'lobby'), None)
            if old:
                old.stop()
            await channel.get_partial_message(room['message_id']).edit(
                embed=self.battle_embed(room, battle), view=self.view(room),
                allowed_mentions=discord.AllowedMentions.none())
            return room

    async def cancel_lobby(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or room['status'] != 'lobby':
                raise TotalRaidError('只有尚未開戰的房間可以關閉。')
            if member.id != room['host_id']:
                raise TotalRaidError('只有開房的房主可以關閉房間。')
            room['status'] = 'cancelled'
            self.repo.save(room)
            self.views.pop((room_id, 'lobby'), None)
            return room

    def running_battle(self, room_id, user_id):
        room = self.repo.get(room_id)
        if not room or room['status'] != 'running':
            raise TotalRaidError('這場總力戰目前不能選擇行動。')
        if user_id not in room['members']:
            raise TotalRaidError('你不在這個總力戰隊伍中。')
        battle = load_total_battle(room['battle'])
        if user_id not in battle.living_player_ids():
            raise TotalRaidError('你目前已經倒下。')
        return room, battle

    async def submit_action(self, room_id, user_id, action, target, skill_slot):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            battle.submit(user_id, action, target, skill_slot)
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            if battle.ready_to_resolve():
                await self._resolve(room, battle)
            else:
                await self._edit_public(room, battle)

    async def _resolve(self, room, battle, timeout=False):
        battle.resolve(use_defaults=timeout)
        room['battle'] = dump_total_battle(battle)
        if battle.result:
            room['status'] = 'completed'
            room['round_deadline'] = None
            view = self.views.pop((room['id'], 'running'), None)
            if view:
                view.stop()
        else:
            room['round_deadline'] = time.time() + self.settings.action_timeout_seconds
        self.repo.save(room)
        await self._edit_public(room, battle)

    async def _edit_public(self, room, battle):
        channel = self.bot.get_channel(room['channel_id'])
        if not isinstance(channel, discord.TextChannel):
            return
        view = self.view(room) if room['status'] in ('lobby', 'running') else None
        await channel.get_partial_message(room['message_id']).edit(
            embed=self.battle_embed(room, battle), view=view,
            allowed_mentions=discord.AllowedMentions.none())

    def lobby_embed(self, room):
        roster = '\n'.join(f'<@{user_id}>' for user_id in room['members'])
        embed = discord.Embed(
            title=f'總力戰測試｜{room["boss"]} #{room["number"]}', color=0x8B5CF6,
            description='加入隊伍後，由房主決定何時開始。測試版不消耗噴漆罐套組，也不發放獎勵。',
        )
        embed.add_field(name='房主', value=f'<@{room["host_id"]}>')
        embed.add_field(name='隊伍', value=f'{len(room["members"])}/{self.settings.max_participants}', inline=True)
        embed.add_field(name='參戰成員', value=roster or '尚無成員', inline=False)
        embed.set_footer(text='房主建立房間時會自動加入；目前最多六人。')
        return embed

    def battle_embed(self, room, battle):
        status = battle.result or f'第 {battle.planning_round} 回合・選擇行動'
        embed = discord.Embed(
            title=f'{room["boss"]} #{room["number"]}｜{status}', color=0xDC2626,
            description='\n'.join(battle.log[-10:])[-2500:] or '戰鬥開始！',
        )
        enemies = '\n'.join(
            f'{fighter.name}：{fighter.hp:,}/{fighter.stats["HP"]:,}'
            for fighter in battle.fighters if fighter.team == 1)
        embed.add_field(name='Boss HP', value=enemies, inline=False)
        waiting = battle.waiting_player_ids() if not battle.result else set()
        roster = []
        for fighter in (item for item in battle.fighters if item.team == 0):
            marker = '💀' if fighter.hp <= 0 else ('⌛' if fighter.user_id in waiting else '✅')
            roster.append(f'{marker} <@{fighter.user_id}>：{fighter.hp:,}/{fighter.stats["HP"]:,}')
        embed.add_field(name='隊伍狀態', value='\n'.join(roster), inline=False)
        if not battle.result:
            intent = battle.intent()
            embed.add_field(name=f'敵方預告｜{intent.name}', value=intent.description, inline=False)
            embed.add_field(name='行動期限', value=f'<t:{int(room["round_deadline"])}:R>', inline=False)
            embed.set_footer(text='點擊下方按鈕開啟私人面板；✅ 僅表示已完成選擇，不公開實際行動。')
        else:
            embed.set_footer(text='測試版不發放獎勵；頻道目前保留供檢查戰報。')
        return embed

    @tasks.loop(seconds=5)
    async def tick(self):
        if not self.bot.is_ready():
            return
        now = time.time()
        for room in self.repo.active():
            if not isinstance(self.bot.get_channel(room['channel_id']), discord.TextChannel):
                room['status'] = 'cancelled'
                self.repo.save(room)
                continue
            if room['status'] != 'running' or now < room.get('round_deadline', now + 1):
                continue
            async with self.lock(room['id']):
                room = self.repo.get(room['id'])
                if not room or room['status'] != 'running' or now < room.get('round_deadline', now + 1):
                    continue
                try:
                    await self._resolve(room, load_total_battle(room['battle']), timeout=True)
                except discord.NotFound:
                    room['status'] = 'cancelled'
                    self.repo.save(room)
                except Exception:
                    logger.exception('Total raid update failed: %s', room['id'])

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
