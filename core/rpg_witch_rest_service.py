"""Discord rooms for Witch Rest Ritual."""
import asyncio
from dataclasses import asdict
import io
import logging
import random
import time

import discord
from discord.ext import tasks

from core.rpg import level_for
from core.rpg_character import CharacterError, ITEMS
from core.rpg_total_battle import dump_total_battle, load_total_battle
from core.rpg_total_raids import (
    TotalRaidService,
    WitchBattleView,
    WITCH_BOSS,
    WITCH_ROUND_SECONDS,
    complete_battle_report,
)
from core.rpg_witch_rest import MIN_LEVEL, WITCHES
from core.rpg_witch_rest_battle import auto_battle_from_participants, manual_battle_from_participants
from core.rpg_room_occupancy import occupied_room, room_lock


logger = logging.getLogger(__name__)


class WitchRestLobbyView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    async def change(self, interaction, leave=False):
        try:
            room = await self.service.change_member(self.room_id, interaction.user, leave=leave)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.service.lobby_embed(room), view=self)

    @discord.ui.button(label='加入隊伍', style=discord.ButtonStyle.success,
                       custom_id='witch_rest:lobby:join')
    async def join(self, interaction, _button):
        await self.change(interaction)

    @discord.ui.button(label='退出隊伍', style=discord.ButtonStyle.secondary,
                       custom_id='witch_rest:lobby:leave')
    async def leave(self, interaction, _button):
        await self.change(interaction, True)

    @discord.ui.button(label='開始戰鬥', style=discord.ButtonStyle.danger,
                       custom_id='witch_rest:lobby:start')
    async def begin(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.service.begin(self.room_id, interaction.user)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.stop()
        mode = '自動' if room['enrage'] < 100 else '手動'
        message = (f'已開始{"練習" if room["practice"] else "正式"}{mode}戰鬥。'
                   if room['status'] == 'running' else
                   f'已結算{"練習" if room["practice"] else "正式"}戰鬥：{room["result"]}。')
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label='關閉房間', style=discord.ButtonStyle.secondary,
                       custom_id='witch_rest:lobby:close')
    async def close(self, interaction, _button):
        try:
            await self.service.close_room(self.room_id, interaction.user)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content='魔女安息儀式房間已關閉。', embed=None, view=None)


class WitchRestService:
    def __init__(self, cog):
        self.cog, self.bot, self.repo = cog, cog.bot, cog.witch_rest
        self.locks, self.views = {}, {}
        self.private_panels, self.private_cleanup_tasks = {}, set()

    def lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    def start(self):
        for room in self.repo.active_rooms():
            if room['status'] == 'lobby' and (room.get('index_message_id') or room.get('message_id')):
                view = WitchRestLobbyView(self, room['id'])
                self.views[room['id']] = view
                self.bot.add_view(view, message_id=room.get('index_message_id') or room['message_id'])
            elif (room['status'] == 'running' and room.get('message_id') and room.get('battle')
                  and room['battle'].get('mode') != 'witch_rest_auto'):
                view = WitchBattleView(self, room['id'])
                self.views[(room['id'], 'running')] = view
                self.bot.add_view(view, message_id=room['message_id'])
        self.tick.start()

    def close(self):
        self.tick.cancel()
        for view in self.views.values():
            view.stop()
        for panel in self.private_panels.values():
            panel['view'].stop()

    @tasks.loop(seconds=1)
    async def tick(self):
        now = time.time()
        for current in self.repo.active_rooms():
            if current['status'] != 'running':
                continue
            async with self.lock(current['id']):
                room = self.repo.get(current['id'])
                if not room or room['status'] != 'running':
                    continue
                battle = load_total_battle(room['battle'])
                if room['battle'].get('mode') == 'witch_rest_auto':
                    if room.get('round_deadline') and now >= room['round_deadline']:
                        await self._step_auto(room, battle)
                    continue
                if room.get('round_deadline') and now >= room['round_deadline']:
                    await self._resolve(room, battle, timeout=True)
        for room in self.repo.expire_rooms():
            channel = self.bot.get_channel(room.get('channel_id'))
            if isinstance(channel, (discord.TextChannel, discord.Thread)) and room.get('message_id'):
                try:
                    await channel.get_partial_message(room['message_id']).edit(
                        content='房間已因 30 分鐘沒有互動而關閉。', embed=None, view=None)
                except discord.HTTPException:
                    pass
            await self._close_index(room, '魔女安息儀式房間已逾時關閉。')
            await self._archive_thread(room, '魔女安息儀式房間已逾時')
        for room in self.repo.pending_reports():
            try:
                await self._post_battle_report(room)
            except discord.HTTPException:
                logger.exception('Witch rest report delivery retry failed: %s', room['id'])
        for room in self.repo.archives_due(now=now):
            if not room.get('report_message_id'):
                continue
            await self._archive_thread(room, '魔女安息儀式結束後已保留一天')
            self.repo.mark_archived(room['id'])

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    async def create_room(self, interaction, witch_id, enrage, practice=False):
        level = level_for(self.cog.store.xp(interaction.guild_id, interaction.user.id))
        if level < MIN_LEVEL:
            raise CharacterError(f'魔女安息儀式需要 Lv.{MIN_LEVEL}。')
        async with room_lock(self.cog):
            if occupied_room(self.cog, interaction.guild_id, interaction.user.id):
                raise CharacterError('你已在另一個手動房間中。')
            room = self.repo.create_room(
                interaction.guild_id, interaction.user.id, witch_id, enrage, practice)
        async with self.lock(('guild', interaction.guild_id)):
            witch = WITCHES[witch_id]
            name = f'{"練習-" if practice else ""}魔女安息-{witch.name}-{room["number"]}'
            parents = [channel for channel in await self.cog.total_raids.witch_announcement_channels()
                       if channel.guild.id == interaction.guild_id]
            if not parents:
                self.repo.close_room(room['id'], interaction.user.id)
                raise CharacterError('找不到魔女試煉大廳頻道。')
            parent = parents[0]
            index = thread = None
            view = WitchRestLobbyView(self, room['id'])
            try:
                index = await parent.send(
                    embed=self.lobby_embed(room), view=view,
                    allowed_mentions=discord.AllowedMentions.none())
                thread = await parent.create_thread(
                    name=name[:100], type=discord.ChannelType.private_thread,
                    invitable=False, auto_archive_duration=1440,
                    reason=f'{interaction.user} 建立魔女安息儀式私人房')
                await thread.add_user(interaction.user)
                message = await thread.send(embed=self.thread_lobby_embed(room),
                                            allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                self.repo.close_room(room['id'], interaction.user.id)
                if index is not None:
                    try:
                        await index.edit(content='魔女安息儀式房間建立失敗。', embed=None, view=None)
                    except discord.HTTPException:
                        pass
                if thread is not None:
                    try:
                        await thread.edit(archived=True, locked=True,
                                          reason='魔女安息儀式建立失敗')
                    except discord.HTTPException:
                        pass
                raise
            room = self.repo.attach_room(
                room['id'], thread.id, message.id, parent_channel_id=parent.id,
                index_message_id=index.id)
            self.views[room['id']] = view
            return room, thread

    async def change_member(self, room_id, member, *, leave=False):
        async with self.lock(room_id):
            level = level_for(self.cog.store.xp(member.guild.id, member.id))
            async with room_lock(self.cog):
                if not leave and occupied_room(
                        self.cog, member.guild.id, member.id, exclude=('witch_rest', room_id)):
                    raise CharacterError('你已在另一個手動房間中。')
                room = self.repo.change_member(room_id, member.id, level, leave=leave)
            channel = self.bot.get_channel(room['channel_id'])
            if isinstance(channel, discord.Thread):
                if leave:
                    await channel.remove_user(member)
                else:
                    await channel.add_user(member)
            elif isinstance(channel, discord.TextChannel):
                await channel.set_permissions(member, overwrite=None if leave else discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True),
                    reason='更新魔女安息儀式隊員')
            return room

    async def begin(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.room(room_id)
            if not room:
                raise CharacterError('找不到這個房間。')
            channel = self.bot.get_channel(room['channel_id'])
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise CharacterError('找不到戰鬥頻道。')
            participants = []
            for user_id in room['members']:
                player = channel.guild.get_member(user_id)
                if player is None or player.bot:
                    raise CharacterError('有隊員已離開伺服器，無法建立開戰快照。')
                state = self.cog.characters.snapshot(room['guild_id'], user_id)
                passive = self.cog.tactics.passive(room['guild_id'], user_id, state['job'])
                participants.append({'id': user_id, 'name': player.display_name[:16], 'state': state,
                    'rules': [asdict(rule) for rule in self.cog.tactics.rules(
                        room['guild_id'], user_id, state['job'])],
                    'passive_id': passive.id if passive else None,
                    'basic_target': self.cog.tactics.basic_target(room['guild_id'], user_id, state['job'])})
            room = self.repo.start_room(room_id, member.id, participants)
            await self._close_index(room, '魔女安息儀式已開始。')
            old = self.views.pop(room_id, None)
            if old:
                old.stop()
            if room['enrage'] >= 100:
                battle = manual_battle_from_participants(
                    participants, room['witch_id'], room['enrage'], random.randrange(2**31))
                room.update(boss=WITCH_BOSS, battle=dump_total_battle(battle),
                            round_deadline=time.time() + WITCH_ROUND_SECONDS,
                            action_drafts={}, surrender_votes=[], log_page=0, status_page=0)
                self.repo.save(room)
                await self._edit_public(room, battle)
                return room
            battle = auto_battle_from_participants(
                participants, room['witch_id'], room['enrage'], random.randrange(2**31))
            room.update(battle=dump_total_battle(battle), round_deadline=time.time() + 2)
            self.repo.save(room)
            await self._edit_auto(room, battle)
            return room

    running_battle = TotalRaidService.running_battle
    player_action_text = TotalRaidService.player_action_text
    confirm_action = TotalRaidService.confirm_action
    enable_auto_action = TotalRaidService.enable_auto_action
    takeover_action = TotalRaidService.takeover_action
    check_witch_deadline = TotalRaidService.check_witch_deadline
    private_choice = TotalRaidService.private_choice
    vote_surrender = TotalRaidService.vote_surrender
    refresh_private_panel = TotalRaidService.refresh_private_panel
    refresh_private_panels = TotalRaidService.refresh_private_panels

    def view(self, room):
        key = (room['id'], 'running')
        if key not in self.views:
            self.views[key] = WitchBattleView(self, room['id'])
        return self.views[key]

    def _settle_rewards(self, room):
        if room['result'] != '勝利' or room['practice']:
            return []
        channel = self.bot.get_channel(room['channel_id'])
        guild = channel.guild if isinstance(channel, (discord.TextChannel, discord.Thread)) else None
        rewards = []
        for index, participant in enumerate(room['participants']):
            member = guild.get_member(participant['id']) if guild else None
            if member is None or member.bot or member.status == discord.Status.offline:
                continue
            reward = self.repo.settle(
                room['id'], room['guild_id'], participant['id'], room['witch_id'],
                participant['state']['job'], room['enrage'], seed=f'{room["id"]}:{index}')
            rewards.append((participant['id'], reward))
        return rewards

    async def _step_auto(self, room, battle):
        battle.step()
        if not battle.result:
            room.update(battle=dump_total_battle(battle), round_deadline=time.time() + 2)
            self.repo.save(room)
            await self._edit_auto(room, battle)
            return
        snapshot = dump_total_battle(battle)
        snapshot['rounds'] = battle.round  # Preserve the legacy result-summary field.
        room = self.repo.finish_room(room['id'], battle.result, snapshot)
        room['rewards'] = self._settle_rewards(room)
        self.repo.save(room)
        await self._edit_auto(room, battle)
        try:
            await self._post_battle_report(room, battle)
        except discord.HTTPException:
            logger.exception('Witch rest report delivery failed: %s', room['id'])

    async def _resolve(self, room, battle, timeout=False):
        battle.resolve(use_defaults=timeout)
        room.update(battle=dump_total_battle(battle), action_drafts={}, surrender_votes=[],
                    log_page=0, status_page=0)
        if battle.result:
            now = time.time()
            room.update(status='completed', result=battle.result, finished_at=now,
                        archive_at=now + 86_400, expires_at=now + 86_400,
                        thread_archived=False, round_deadline=None)
            rewards = self._settle_rewards(room)
            room['rewards'] = rewards
            view = self.views.pop((room['id'], 'running'), None)
            if view:
                view.stop()
        else:
            room['round_deadline'] = time.time() + WITCH_ROUND_SECONDS
        self.repo.save(room)
        try:
            await self._edit_public(room, battle)
        finally:
            await self.refresh_private_panels(room)
        if battle.result:
            try:
                await self._post_battle_report(room, battle)
            except discord.HTTPException:
                logger.exception('Witch rest report delivery failed: %s', room['id'])

    def _reward_report_lines(self, room):
        if room['practice']:
            return ['練習模式：未消耗討伐之證、未發放獎勵，也未更新進度。']
        lines = []
        for user_id, reward in room.get('rewards', ()):
            drops = [f'{ITEMS[key].name} ×{amount}' for key, amount in reward['items'].items()]
            if reward['gold']:
                drops.append(f'金幣 ×{reward["gold"]:,}')
            if reward['treasure']:
                drops.append(f'秘寶：{ITEMS[reward["treasure"]].name}')
            lines.append(f'{user_id}: ' + '、'.join(drops or ('無額外掉落',)))
        return lines or ['無獎勵。']

    async def _post_battle_report(self, room, battle=None):
        if room.get('report_message_id') or not room.get('battle'):
            return
        channel = self.bot.get_channel(room['channel_id'])
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        snapshot = room['battle']
        if battle is None and snapshot.get('fighters'):
            battle = load_total_battle(snapshot)
        witch = WITCHES[room['witch_id']]
        rounds = battle.round if battle is not None else snapshot.get('rounds', snapshot.get('round', 0))
        header = (f'魔女安息儀式 #{room["number"]}｜{room["result"]}\n'
                  f'{witch.name}・魔女化 {room["enrage"]:,}%｜{rounds} 回合')
        reward_lines = self._reward_report_lines(room)
        if battle is not None:
            report = complete_battle_report(battle, header, reward_lines)
        else:
            # Reports created by older versions only retained a bounded log summary.
            lines = [header, '', '既有紀錄：', *snapshot.get('log', ()), '',
                     '戰鬥結算：', '此戰於完整快照功能啟用前結束，無法回推個人統計。',
                     '', '獎勵：', *reward_lines]
            report = '\n'.join(str(line) for line in lines)
        embed = self.result_embed(room, room.get('rewards', []))
        embed.title = f'魔女安息儀式 #{room["number"]}｜完整戰報'
        embed.set_footer(text='完整逐回合記錄、個人戰鬥統計與獎勵收錄於附件；討論串保留 24 小時。')
        message = await channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(report.encode('utf-8')),
                              filename=f'witch-rest-{room["number"]}-complete.txt'),
            allowed_mentions=discord.AllowedMentions.none())
        room['report_message_id'] = message.id
        self.repo.save(room)
    async def _edit_auto(self, room, battle):
        channel = self.bot.get_channel(room['channel_id'])
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = (self.result_embed(room, room.get('rewards', [])) if battle.result
                 else self.auto_battle_embed(room, battle))
        await channel.get_partial_message(room['message_id']).edit(
            embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())

    async def _edit_public(self, room, battle):
        channel = self.bot.get_channel(room['channel_id'])
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = (self.result_embed(room, room.get('rewards', [])) if battle.result
                 else self.battle_embed(room, battle))
        await channel.get_partial_message(room['message_id']).edit(
            embed=embed, view=self.view(room) if room['status'] == 'running' else None,
            allowed_mentions=discord.AllowedMentions.none())

    async def _close_index(self, room, content):
        parent = self.bot.get_channel(room.get('parent_channel_id'))
        if not isinstance(parent, discord.TextChannel) or not room.get('index_message_id'):
            return
        try:
            await parent.get_partial_message(room['index_message_id']).edit(
                content=content, embed=self.lobby_embed(room), view=None,
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def _archive_thread(self, room, reason):
        channel = self.bot.get_channel(room.get('channel_id'))
        if not isinstance(channel, discord.Thread):
            guild = self.bot.get_guild(room['guild_id']) if hasattr(self.bot, 'get_guild') else None
            if guild and room.get('channel_id'):
                try:
                    channel = await guild.fetch_channel(room['channel_id'])
                except (discord.HTTPException, discord.NotFound):
                    return
        if not isinstance(channel, discord.Thread):
            return
        try:
            if getattr(channel, 'archived', False):
                await channel.edit(archived=False, reason=reason)
            await channel.edit(archived=True, locked=True, reason=reason)
        except discord.HTTPException:
            pass

    def thread_lobby_embed(self, room):
        return discord.Embed(
            title=f'魔女安息儀式 #{room["number"]}', color=0xA855F7,
            description='這是私人作戰討論串。請在大廳房間卡加入隊伍，由房主開始戰鬥。')

    def battle_embed(self, room, battle):
        embed = TotalRaidService.battle_embed(self, room, battle)
        embed.title = embed.title.replace('魔女試煉', '魔女安息儀式')
        prefix = f'{WITCHES[room["witch_id"]].name}・魔女化 {room["enrage"]:,}%'
        embed.description = prefix + (f'\n{embed.description}' if embed.description else '')
        if room['witch_id'] == 'hiro':
            history = battle.mechanics.get('rest_action_history', {})
            lines = [f'死亡回溯：{battle.mechanics.get("hiro_rewinds", 0)}/2']
            for fighter in (item for item in battle.fighters if item.team == 0):
                recent = history.get(str(fighter.user_id), [])[-3:]
                lines.append(f'{fighter.name}：' + '→'.join(recent or ('無',)))
            embed.add_field(name='公開歷史', value='\n'.join(lines)[:1024], inline=False)
        elif battle.mechanics.get('rest_boss_shield'):
            embed.add_field(name='判決護盾', value=f'{battle.mechanics["rest_boss_shield"]:,}', inline=False)
        return embed

    def auto_battle_embed(self, room, battle):
        boss = next((fighter for fighter in battle.fighters if fighter.team == 1), None)
        party = '\n'.join(
            f'{fighter.name}：{max(0, fighter.hp):,}/{fighter.stats["HP"]:,} HP'
            for fighter in battle.fighters if fighter.team == 0)
        embed = discord.Embed(title='魔女安息儀式｜自動戰鬥', color=0xA855F7,
            description=f'{WITCHES[room["witch_id"]].name}・魔女化 {room["enrage"]:,}%｜第 {battle.round} 回合')
        if boss:
            embed.add_field(name='Boss HP', value=f'{max(0, boss.hp):,}/{boss.stats["HP"]:,}', inline=False)
        embed.add_field(name='隊伍狀態', value=party or '無存活隊員', inline=False)
        embed.add_field(name='最近戰況', value='\n'.join(battle.log[-6:])[-1024:] or '準備交戰。', inline=False)
        embed.set_footer(text='自動戰鬥每 2 秒推進一回合，可留在討論串觀看過程。')
        return embed

    async def close_room(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.close_room(room_id, member.id)
            await self._archive_thread(room, '魔女安息儀式房間已關閉')
            return room

    def lobby_embed(self, room):
        witch = WITCHES[room['witch_id']]
        practice = '練習模式｜不收費、不發獎、不更新紀錄' if room['practice'] else '正式挑戰｜開戰時每人收取討伐之證 ×10'
        mode = '自動戰鬥' if room['enrage'] < 100 else '手動戰鬥'
        embed = discord.Embed(title=f'魔女安息儀式 #{room["number"]}', color=0xA855F7,
            description=f'**{witch.name}・魔女化 {room["enrage"]:,}%**\n{mode}｜{practice}')
        embed.add_field(name='房主', value=f'<@{room["host_id"]}>')
        embed.add_field(name='隊伍', value=f'{len(room["members"])}/6')
        embed.add_field(name='參戰成員', value='\n'.join(f'<@{uid}>' for uid in room['members']), inline=False)
        embed.set_footer(text='任一操作後重新計時；連續 30 分鐘沒有互動會自動關閉。')
        return embed

    def result_embed(self, room, rewards):
        practice = '｜練習模式' if room['practice'] else ''
        embed = discord.Embed(title=f'魔女安息儀式｜{room["result"]}{practice}', color=0x22C55E if room['result'] == '勝利' else 0xEF4444)
        rounds = room['battle'].get('rounds', room['battle'].get('round', 0))
        embed.description = f'{WITCHES[room["witch_id"]].name}・魔女化 {room["enrage"]:,}%｜{rounds} 回合'
        if room['practice']:
            embed.add_field(name='練習結算', value='未消耗討伐之證，未發放獎勵，也未更新任何進度。', inline=False)
        elif rewards:
            lines = []
            for user_id, reward in rewards:
                drops = [f'{ITEMS[key].name} ×{amount}' for key, amount in reward['items'].items()]
                if reward['gold']:
                    drops.append(f'金幣 ×{reward["gold"]:,}')
                if reward['treasure']:
                    drops.append(f'秘寶：{ITEMS[reward["treasure"]].name}')
                lines.append(f'<@{user_id}>：' + '、'.join(drops or ('無額外掉落',)))
            embed.add_field(name='個人獎勵', value='\n'.join(lines)[:1024], inline=False)
        embed.add_field(name='戰鬥摘要', value='\n'.join(room['battle'].get('log', [])[-8:])[-1024:] or '無。', inline=False)
        if room.get('archive_at'):
            embed.add_field(name='討論串封存時間', value=f'<t:{int(room["archive_at"])}:R>', inline=False)
        embed.set_footer(text='戰鬥結束後討論串保留 24 小時供隊員聊天，之後鎖定並封存。')
        return embed
