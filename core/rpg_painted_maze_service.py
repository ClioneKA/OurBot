"""Discord-facing private-thread service for the Painted Maze roguelite."""
import asyncio
import io
from dataclasses import asdict
import logging
import time

import discord
from discord.ext import tasks

from core.rpg_character import CharacterError, ITEMS
from core.rpg_crystals import CrystalStore
from core.rpg_painted_maze import (
    COLOR_CONTRACTS, ENTRY_CLOSED_NOTICE, ENTRY_ENABLED, MODE_NAME, paintings_per_stage,
    PaintedMazeError, PaintedMazeStore,
)
from core.rpg_battle import dump_battle, load_battle
from core.rpg_painted_maze_battle import (
    build_final_battle, build_painting_battle, carry_party_state, painting_battle_seed,
)
from core.rpg_painted_maze_views import FinalVoteView
from core.rpg_painted_maze_rest import MazeSkillView
from core.rpg_painted_maze_rewards import CHECKPOINT_REWARDS, PaintedMazeRewardStore


logger = logging.getLogger(__name__)


class MazeLobbyView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    async def _change(self, interaction, leave=False):
        try:
            room = await self.service.change_member(self.room_id, interaction.user, leave=leave)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.service.lobby_embed(room), view=self)

    @discord.ui.button(label='加入隊伍', style=discord.ButtonStyle.success,
                       custom_id='painted_maze:lobby:join')
    async def join(self, interaction, _button):
        await self._change(interaction)

    @discord.ui.button(label='退出隊伍', style=discord.ButtonStyle.secondary,
                       custom_id='painted_maze:lobby:leave')
    async def leave(self, interaction, _button):
        await self._change(interaction, leave=True)

    @discord.ui.button(label='開始探索', style=discord.ButtonStyle.danger,
                       custom_id='painted_maze:lobby:begin')
    async def begin(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.service.begin(self.room_id, interaction.user)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.stop()
        await interaction.followup.send(
            f'{MODE_NAME}已開始，共 {len(room["members"])} 人參戰。', ephemeral=True)

    @discord.ui.button(label='關閉房間', style=discord.ButtonStyle.secondary,
                       custom_id='painted_maze:lobby:close')
    async def close(self, interaction, _button):
        try:
            await self.service.close_room(self.room_id, interaction.user)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content='繪境迷廊房間已關閉。', embed=None, view=None)


class MazeProgressView(discord.ui.View):
    def __init__(self, service, room_id, *, final=False):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id
        room = service.repo.get(room_id)
        self.index = room['boss_index']
        self.advance.label = '開始尾王去留投票' if final else '開始本場戰鬥'
        self.advance.disabled = set(room.get('rest_ready', [])) != set(room['members'])

    @discord.ui.button(label='調整技能', style=discord.ButtonStyle.primary,
                       custom_id='painted_maze:rest:skills')
    async def skills(self, interaction, _button):
        try:
            room, _ = self.service.repo.rest_participant(
                self.room_id, interaction.user.id, expected_index=self.index)
            view = MazeSkillView(self.service, room, interaction)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label='準備完成／取消準備', style=discord.ButtonStyle.success,
                       custom_id='painted_maze:rest:ready')
    async def ready(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.service.lock(self.room_id):
                room = self.service.repo.ready_at_rest(self.room_id, interaction.user.id, expected_index=self.index)
                await self.service._refresh(room)
            notice = '已準備完成。' if interaction.user.id in room['rest_ready'] else '已取消準備。'
        except CharacterError as exc:
            notice = str(exc)
        await interaction.followup.send(notice, ephemeral=True)

    @discord.ui.button(label='討伐下一幅畫作', style=discord.ButtonStyle.danger,
                       custom_id='painted_maze:progress:advance')
    async def advance(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.service.advance(self.room_id, interaction.user, expected_index=self.index)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        notice = '已開啟尾王去留投票。' if room.get('final_vote') else '戰鬥已開始，回合進度會更新在房間面板。'
        await interaction.followup.send(notice, ephemeral=True)


class ContractVoteView(discord.ui.View):
    def __init__(self, service, room):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room['id']
        vote = room['contract_vote']
        self.choice.options = [discord.SelectOption(
            label=COLOR_CONTRACTS[key]['name'], value=key,
            description=COLOR_CONTRACTS[key]['party'][:100]) for key in vote['candidates']]

    @discord.ui.select(placeholder='選擇要投票的色彩契約', min_values=1, max_values=1,
                       custom_id='painted_maze:contract:vote')
    async def choice(self, interaction, select):
        try:
            room = self.service.repo.cast_contract_vote(
                self.room_id, interaction.user.id, select.values[0])
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        name = COLOR_CONTRACTS[select.values[0]]['name']
        await interaction.response.send_message(
            f'已投給【{name}】；截止前可重新選擇。'
            f'（目前 {len(room["contract_vote"]["votes"])} 人已投票）', ephemeral=True)


class PaintedMazeService:
    def __init__(self, cog):
        self.cog, self.bot = cog, cog.bot
        self.repo = PaintedMazeStore(cog.store)
        self.crystals = CrystalStore(cog.store)
        self.rewards = PaintedMazeRewardStore(cog.store)
        self._locks = {}

    def lock(self, room_id):
        return self._locks.setdefault(room_id, asyncio.Lock())

    def start(self):
        for room in self.repo.active():
            if room.get('status') == 'lobby' and room.get('index_message_id'):
                self.bot.add_view(MazeLobbyView(self, room['id']), message_id=room['index_message_id'])
            elif room.get('message_id'):
                view = self.room_view(room)
                if view:
                    self.bot.add_view(view, message_id=room['message_id'])
        self.tick.start()

    def close(self):
        self.tick.cancel()

    async def _thread(self, room):
        channel = self.bot.get_channel(room.get('thread_id'))
        if isinstance(channel, discord.Thread):
            return channel
        guild = self.bot.get_guild(room['guild_id'])
        if guild and room.get('thread_id'):
            try:
                fetched = await guild.fetch_channel(room['thread_id'])
                return fetched if isinstance(fetched, discord.Thread) else None
            except discord.HTTPException:
                return None
        return None

    async def create(self, interaction, entry_item, *, require_entry=True):
        if require_entry and not ENTRY_ENABLED:
            raise PaintedMazeError(ENTRY_CLOSED_NOTICE)
        space = self.cog.spaces.store.get(interaction.guild_id)
        if not space or interaction.channel_id != space.maze_channel_id:
            raise PaintedMazeError('請在「🎨・繪境迷廊」頻道開啟畫作。')
        level = self.cog.characters.snapshot(interaction.guild_id, interaction.user.id)['level']
        room = self.repo.create(interaction.guild_id, interaction.user.id, entry_item, level,
                                channel_id=interaction.channel_id, require_entry=require_entry)
        index = thread = None
        try:
            index = await interaction.channel.send(
                embed=self.lobby_embed(room), view=MazeLobbyView(self, room['id']),
                allowed_mentions=discord.AllowedMentions.none())
            thread = await interaction.channel.create_thread(
                name=f'{MODE_NAME} #{room["number"]}', type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=1440, reason='開啟繪境迷廊私人房')
            await thread.add_user(interaction.user)
            message = await thread.send(embed=self.room_embed(room))
            return self.repo.attach_discord(
                room['id'], channel_id=interaction.channel_id, thread_id=thread.id,
                index_message_id=index.id, message_id=message.id)
        except Exception:
            closed = self.repo.close(room['id'], interaction.user.id)
            if index is not None:
                try:
                    await index.edit(embed=self.lobby_embed(closed), view=None)
                except discord.HTTPException:
                    pass
            if thread is not None:
                try:
                    await thread.edit(archived=True, locked=True,
                                      reason='繪境迷廊建立失敗，封存殘留討論串')
                except discord.HTTPException:
                    pass
            raise

    async def change_member(self, room_id, member, *, leave=False):
        if member.bot:
            raise PaintedMazeError('機器人不能進入繪境迷廊。')
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not leave and room.get('requires_entry', True) and not ENTRY_ENABLED:
                raise PaintedMazeError(ENTRY_CLOSED_NOTICE)
            level = self.cog.characters.snapshot(room['guild_id'], member.id)['level']
            room = self.repo.change_member(room_id, member.id, level, leave=leave)
            thread = await self._thread(room)
            if thread:
                if leave:
                    await thread.remove_user(member)
                else:
                    await thread.add_user(member)
                    await thread.send(f'<@{member.id}> 進入了未完成的畫布。')
            return room

    async def begin(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or member.id != room['host_id']:
                raise PaintedMazeError('只有房主可以開始繪境迷廊。')
            if room.get('requires_entry', True) and not ENTRY_ENABLED:
                raise PaintedMazeError(ENTRY_CLOSED_NOTICE)
            thread = await self._thread(room)
            if not thread:
                raise PaintedMazeError('找不到房間的私人討論串，尚未消耗畫作或攜帶效果。')
            if (room.get('requires_entry', True) and self.cog.characters.inventory_counts(
                    room['guild_id'], room['host_id']).get(room['entry_item'], 0) < 1):
                raise PaintedMazeError('房主已沒有這張入場畫作。')
            guild = self.bot.get_guild(room['guild_id'])
            participants = []
            for user_id in room['members']:
                player = guild.get_member(user_id) if guild else None
                if player is None or player.bot:
                    raise PaintedMazeError('有隊員已離開伺服器，請先調整隊伍。')
                state = self.cog.characters.snapshot(room['guild_id'], user_id)
                passive = self.cog.tactics.passive(room['guild_id'], user_id, state['job'])
                participants.append({
                    'id': user_id, 'name': player.display_name[:16], 'state': state,
                    'rules': [asdict(rule) for rule in self.cog.tactics.rules(
                        room['guild_id'], user_id, state['job'])],
                    'passive_id': passive.id if passive else None,
                    'basic_target': self.cog.tactics.basic_target(room['guild_id'], user_id, state['job']),
                })
            user_ids = [item['id'] for item in participants]
            fortunes = self.cog.divinations.prepare_for_raid(room_id, room['guild_id'], user_ids)
            drinks = self.cog.tavern.store.prepare_for_raid(room_id, room['guild_id'], user_ids)
            meals = self.cog.provisions.prepare_for_raid(
                room_id, room['guild_id'], user_ids,
                preserve_users=[uid for uid in user_ids
                                if fortunes.get(uid, {}).get('id') == 'temperance'])
            for participant in participants:
                uid = participant['id']
                if uid in fortunes:
                    participant['fortune'] = fortunes[uid]
                if uid in drinks:
                    participant['tavern'] = drinks[uid]
                if uid in meals:
                    participant['meal'] = meals[uid]
            room = self.repo.begin(room_id, member.id, participants)
            await thread.get_partial_message(room['message_id']).edit(
                embed=self.room_embed(room), view=MazeProgressView(self, room_id))
            channel = self.bot.get_channel(room['channel_id'])
            if channel:
                await channel.get_partial_message(room['index_message_id']).edit(
                    embed=self.lobby_embed(room), view=None)
            return room

    async def advance(self, room_id, member, *, expected_index=None):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or member.id not in room.get('members', ()):
                raise PaintedMazeError('你不在這個繪境迷廊隊伍中。')
            if room['status'] != 'running' or room.get('battle'):
                raise PaintedMazeError('目前不能推進繪境迷廊。')
            self.repo.rest_participant(room_id, member.id, expected_index=expected_index)
            if set(room.get('rest_ready', [])) != set(room['members']):
                raise PaintedMazeError('請等待全隊在休息點確認準備完成。')
            if room['boss_index'] == len(room['paintings']):
                room = self.repo.ensure_final_vote(room_id)
                await self._refresh(room)
                return room
            if room['boss_index'] < len(room['paintings']):
                index = room['boss_index']
                battle = build_painting_battle(room['participants'], room['paintings'][index],
                    painting_battle_seed(room['seed'], index), room.get('contracts', ()), room.get('party_state'))
                data, delay = dump_battle(battle), 2
            room = self.repo.start_battle(room_id, member.id, data, deadline=time.time() + delay)
            await self._refresh(room)
            return room

    async def _step_battle(self, room):
        data = room['battle']
        final = room['boss_index'] == len(room['paintings'])
        battle = load_battle(data)
        if data.get('mode') == 'maze_final':
            # Resume a saved interactive encounter using automatic Noah actions.
            battle.fighters = [f for f in battle.fighters if f.job != '構圖錨點']
            for fighter in battle.fighters:
                if fighter.is_boss:
                    fighter.job = '城崎諾亞'
        old_round = battle.round
        if not battle.result:
            battle.step()
        snapshot = dump_battle(battle)
        room = self.repo.save_battle(room['id'], snapshot, expected_round=old_round,
                                    deadline=time.time() + 2)
        if battle.result:
            party = carry_party_state(battle, room['boss_index'] + 1, room.get('contracts', ()),
                stage_end=not final and (room['boss_index'] + 1) % paintings_per_stage(room) == 0)
            if final:
                room = self.repo.settle_final(room['id'], room['host_id'], battle.result, snapshot, party)
            else:
                room = self.repo.settle_painting(room['id'], room['host_id'], room['boss_index'],
                                                battle.result, snapshot, party)
            room = self.recover_rewards(room['id'])
        await self._refresh(room)
        if room['status'] in ('completed', 'failed'):
            self.cog.divinations.clear_raid(room['id'])
            await self._archive(room)

    def room_view(self, room):
        if room['status'] == 'contract':
            return ContractVoteView(self, room)
        if room['status'] != 'running':
            return None
        if room.get('battle'):
            return None
        vote = room.get('final_vote')
        if vote and not vote.get('result'):
            return FinalVoteView(self, room['id'])
        return MazeProgressView(self, room['id'], final=room['boss_index'] == len(room['paintings']))

    async def _post_battle_report(self, room):
        if room['status'] in ('lobby', 'running', 'contract') or room.get('report_message_id'):
            return
        thread = await self._thread(room)
        if not thread:
            return
        reports = list(room.get('battle_reports', ()))
        if not reports and room.get('last_battle'):
            reports.append({'title': '既有房間最後一戰', 'battle': room['last_battle']})
        if room.get('battle'):
            reports.append({'title': '結束時尚未完成的戰鬥', 'battle': room['battle']})
        if not reports:
            return
        lines, summaries = [], []
        for report in reports:
            battle = report['battle']
            summary = f'{report["title"]}｜{battle.get("result") or "中止"}｜{battle.get("round", 0)} 回合'
            summaries.append(summary)
            lines.extend([summary, *[str(line) for line in battle.get('log', ())], ''])
        embed = discord.Embed(title=f'{MODE_NAME} #{room["number"]}｜整趟戰報',
            description='\n'.join(summaries)[:4000], color=0x7C3AED)
        embed.set_footer(text='完整逐回合記錄收錄於附件；探索途中不發送戰報。')
        message = await thread.send(embed=embed,
            file=discord.File(io.BytesIO('\n'.join(lines).encode('utf-8')),
                              filename=f'maze-{room["number"]}-complete.txt'),
            allowed_mentions=discord.AllowedMentions.none())
        self.repo.mark_report_sent(room['id'], message.id)
        room['report_message_id'] = message.id

    def recover_rewards(self, room_id):
        """Drain the room's transactional outbox; every individual seal is idempotent."""
        room = self.repo.get(room_id)
        if room.get('reward_policy') == 'escrow_v2' and room['status'] in ('lobby', 'running', 'contract'):
            return room
        for due in list(room.get('reward_due', ())):
            kind, checkpoint = due['kind'], due['checkpoint']
            if kind == 'stage':
                self.crystals.seal_stage(room_id, checkpoint)
                self.rewards.seal_currency(room_id, checkpoint)
            elif kind == 'final':
                self.rewards.seal_currency(room_id, checkpoint)
                if room['route'] == 'noah':
                    self.rewards.seal_noah_clear(room_id)
                else:
                    self.crystals.seal_shadow_bonus(room_id)
            self.repo.mark_reward_complete(room_id, kind, checkpoint)
            room = self.repo.get(room_id)
        return room

    async def close_room(self, room_id, member, *, administrator=False):
        async with self.lock(room_id):
            room = self.repo.close(room_id, member.id, administrator=administrator)
            room = self.recover_rewards(room_id)
            self.cog.divinations.clear_raid(room_id)
            await self._refresh(room)
            await self._archive(room)
            return room

    async def _refresh(self, room):
        channel = self.bot.get_channel(room.get('channel_id'))
        if channel and room.get('index_message_id'):
            try:
                await channel.get_partial_message(room['index_message_id']).edit(
                    embed=self.lobby_embed(room),
                    view=MazeLobbyView(self, room['id']) if room['status'] == 'lobby' else None,
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                pass
        thread = await self._thread(room)
        if not thread or not room.get('message_id'):
            return
        view = self.room_view(room)
        await thread.get_partial_message(room['message_id']).edit(
            embed=self.room_embed(room), view=view,
            allowed_mentions=discord.AllowedMentions.none())

    async def _archive(self, room):
        await self._post_battle_report(room)
        thread = await self._thread(room)
        if thread and not thread.archived:
            await thread.edit(archived=True, locked=True, reason='繪境迷廊已結束')
        self.repo.mark_terminal_refreshed(room['id'])

    def lobby_embed(self, room):
        route = '繪畫魔女．城崎諾亞' if room['route'] == 'noah' else '繪畫之影'
        state = {
            'lobby': '等待中', 'running': '進行中', 'contract': '契約投票中',
            'completed': '已完成', 'failed': '挑戰失敗', 'expired': '已逾期', 'retreated': '已撤退',
            'cancelled': '已關閉', 'admin_ended': '管理員已結束',
        }.get(room['status'], room['status'])
        return discord.Embed(
            title=f'{MODE_NAME} #{room["number"]}｜{state}', color=0xA855F7,
            description=(f'房主：<@{room["host_id"]}>\n路線：**{route}**\n'
                         f'入場：{"開始時消耗畫作" if room.get("requires_entry", True) else "管理員測試（免畫作、無任何獎勵）"}\n'
                         f'隊伍：{len(room["members"])}/8\n'
                         + '\n'.join(f'• <@{uid}>' for uid in room['members'])))

    def room_embed(self, room):
        if room.get('battle') and room['status'] == 'running':
            data = room['battle']
            battle = load_battle(data)
            embed = discord.Embed(title=f'{MODE_NAME} #{room["number"]}｜第 {battle.round} 回合',
                description='\n'.join(battle.log[-12:])[-3000:] or '戰鬥即將開始。', color=0xDC2626)
            for team, name in ((1, '敵方狀態'), (0, '隊伍狀態')):
                embed.add_field(name=name, value='\n'.join(
                    f'{f.name}：{f.hp:,}/{f.stats["HP"]:,} HP' for f in battle.fighters if f.team == team)[:1024],
                    inline=False)
            embed.set_footer(text='約每 2 秒推進一回合；戰鬥中無法調整技能。')
            return embed
        embed = discord.Embed(title=f'{MODE_NAME} #{room["number"]}', color=0x7C3AED)
        if room['status'] == 'lobby':
            embed.description = '這是私人作戰討論串。隊員可在此調整戰術，由房主在大廳面板開始。'
            embed.add_field(name='保留時間', value=f'<t:{int(room["expires_at"])}:R>')
            return embed
        if room.get('last_battle'):
            embed.add_field(name='上一戰', value=(
                f'{room["last_battle"].get("result", room.get("final_battle", {}).get("result", "已結算"))}'
                f'｜{room["last_battle"].get("round", 0)} 回合'), inline=False)
        embed.add_field(name='畫作進度', value=f'{room["boss_index"]}/{len(room["paintings"])}', inline=True)
        contracts = room.get('contracts', ())
        embed.add_field(name='色彩契約', value='\n'.join(
            COLOR_CONTRACTS[key]['name'] for key in contracts) or '尚未選擇', inline=True)
        if contracts:
            embed.add_field(name='尾王反噬（三份契約皆生效，同色疊層）', value='\n'.join(
                f'{index}. {COLOR_CONTRACTS[key]["name"]}：{COLOR_CONTRACTS[key]["backlash"]}'
                for index, key in enumerate(contracts, 1)), inline=False)
        party_lines = []
        participants = {item['id']: item for item in room.get('participants', ())}
        for user_id in room.get('members', ()):
            state = room.get('party_state', {}).get(str(user_id), {})
            participant = participants.get(user_id, {})
            crystals = participant.get('state', {}).get('crystal_effects', ())
            hp, maximum = state.get('hp', 0), state.get('max_hp', 0)
            party_lines.append(
                f'{"💀" if hp <= 0 else "🟢"} <@{user_id}>：{hp:,}/{maximum:,} HP'
                f'｜{len(crystals)} 顆結晶')
        if party_lines:
            embed.add_field(name='隊伍狀態', value='\n'.join(party_lines)[:1024], inline=False)
        if not room.get('requires_entry', True):
            embed.add_field(name='無獎勵測試', value='本場不發放經驗、金幣、結晶或裝備，也不計入首次通關。', inline=False)
        elif room.get('reward_policy') == 'escrow_v2':
            stage = room.get('stage', 0)
            xp = sum(CHECKPOINT_REWARDS[i][0] for i in range(1, stage + 1))
            gold = sum(CHECKPOINT_REWARDS[i][1] for i in range(1, stage + 1))
            if room['status'] in ('running', 'contract'):
                embed.add_field(name='每人累積掉落（離場時發放）',
                    value=f'結晶 ×{stage}｜基礎 {xp:,} XP、{gold:,} 金幣（另計個人加成）', inline=False)
            else:
                payouts = self.rewards.currency_rewards(room['id'])
                totals = {}
                for reward in payouts:
                    total = totals.setdefault(reward['user_id'], [0, 0])
                    total[0] += reward['xp']
                    total[1] += reward['gold']
                kept = (stage + 1) // 2 if room.get('loot_percent') == 50 else stage
                lines = [f'<@{uid}>：{amount[0]:,} XP、{amount[1]:,} 金幣、幕間結晶 ×{kept}'
                         for uid, amount in totals.items()]
                if lines:
                    embed.add_field(name='離場發放', value='\n'.join(lines)[:1024], inline=False)
        if room['status'] == 'contract':
            vote = room['contract_vote']
            lines = [f'**{COLOR_CONTRACTS[key]["name"]}**\n'
                     f'隊伍：{COLOR_CONTRACTS[key]["party"]}\n'
                     f'反噬：{COLOR_CONTRACTS[key]["backlash"]}' for key in vote['candidates']]
            for offset in range(0, len(lines), 3):
                embed.add_field(name=f'第 {vote["round"]} 次契約投票（{offset // 3 + 1}）',
                                value='\n\n'.join(lines[offset:offset + 3]), inline=False)
            embed.add_field(name='截止', value=f'<t:{int(vote["deadline"])}:R>')
        elif room['status'] == 'running':
            if not room.get('final_vote'):
                ready = set(room.get('rest_ready', []))
                embed.add_field(name='戰前休息點', value=(
                    '可調整主動／被動技能與自動施放規則；職業、裝備及攜帶效果固定。\n'
                    '全隊確認準備後才能繼續，修改技能會取消自己的準備狀態。'), inline=False)
                embed.add_field(name=f'準備狀態 {len(ready)}/{len(room["members"])}', value='\n'.join(
                    f'{"✅" if uid in ready else "⏳"} <@{uid}>' for uid in room['members']), inline=False)
            if room['boss_index'] < len(room['paintings']):
                per_stage = paintings_per_stage(room)
                stage_start = room['boss_index'] // per_stage * per_stage
                lines = []
                for index in range(stage_start, stage_start + per_stage):
                    painting = room['paintings'][index]
                    marker = '✅' if index < room['boss_index'] else ('➡️' if index == room['boss_index'] else '⬜')
                    lines.append(f'{marker} {painting["name"]}｜T{painting["tier"]}')
                embed.add_field(name=f'第 {stage_start // per_stage + 1} 幕路線',
                                value='\n'.join(lines), inline=False)
            else:
                boss = ('繪畫魔女．城崎諾亞'
                        if room['route'] == 'noah' else '繪畫之影')
                embed.add_field(name='最終畫室', value=f'{boss}｜T70｜自動戰鬥', inline=False)
                vote = room.get('final_vote')
                if vote and not vote.get('result'):
                    entering = sum(value == 'enter' for value in vote['votes'].values())
                    leaving = sum(value == 'retreat' for value in vote['votes'].values())
                    risk = ('管理員測試場不發放任何獎勵。' if not room.get('requires_entry', True) else
                            '撤退保留全部；進入後未通關，累積結晶、金幣及經驗減半，結晶保留數向上取整。'
                            if room.get('reward_policy') == 'escrow_v2' else
                            '舊版房間已發放的幕間掉落不追扣。')
                    embed.add_field(name='全隊去留投票', value=(
                        f'挑戰 {entering} 票｜撤退 {leaving} 票｜截止 <t:{int(vote["deadline"])}:R>\n'
                        '超過全隊半數同意才進入；平票或同意不足則撤退。\n'
                        + risk), inline=False)
        else:
            embed.add_field(name='結果', value=room.get('end_reason', room['status']), inline=False)
            if room['status'] == 'completed' and room['route'] == 'noah':
                embed.add_field(name='菁英裝備', value=(
                    '首次通關者會取得本職菁英裝備自選箱，可從背包使用；'
                    '已有通關紀錄者已隨機發放。'), inline=False)
        embed.set_footer(text='房間最長保留 24 小時；結束後討論串會封存但隊員仍可查看。')
        return embed

    @tasks.loop(seconds=1)
    async def tick(self):
        if not self.bot.is_ready():
            return
        now = time.time()
        for pending in self.repo.rooms_with_rewards_due():
            try:
                self.recover_rewards(pending['id'])
            except Exception:
                logger.exception('Painted Maze reward recovery failed: %s', pending['id'])
        for room in self.repo.resolve_contracts_due(now=now):
            try:
                await self._refresh(room)
            except discord.HTTPException:
                logger.exception('Painted Maze vote refresh failed: %s', room['id'])
        for room in self.repo.expire_due(now=now):
            try:
                room = self.recover_rewards(room['id'])
                self.cog.divinations.clear_raid(room['id'])
                await self._refresh(room)
                await self._archive(room)
            except discord.HTTPException:
                logger.exception('Painted Maze expiry refresh failed: %s', room['id'])
        for active in self.repo.active():
            try:
                async with self.lock(active['id']):
                    room = self.repo.get(active['id'])
                    if room['status'] != 'running':
                        continue
                    vote = room.get('final_vote')
                    if vote and not vote.get('result') and now >= vote['deadline']:
                        room = self.repo.resolve_final_vote(room['id'], now=now)
                        if room['status'] == 'retreated':
                            room = self.recover_rewards(room['id'])
                            self.cog.divinations.clear_raid(room['id'])
                            await self._refresh(room)
                            await self._archive(room)
                            continue
                    if (room.get('final_vote', {}).get('result') == 'enter'
                            and not room.get('battle') and not room.get('final_battle')):
                        battle = build_final_battle(room)
                        room = self.repo.start_battle(room['id'], room['host_id'], dump_battle(battle),
                                                     deadline=time.time() + 2)
                        await self._refresh(room)
                    elif room.get('battle') and (room['battle'].get('result') or now >= room['battle_deadline']):
                        await self._step_battle(room)
            except Exception:
                logger.exception('Painted Maze round update failed: %s', active['id'])
        for room in self.repo.terminal_refreshes():
            try:
                room = self.recover_rewards(room['id'])
                self.cog.divinations.clear_raid(room['id'])
                await self._refresh(room)
                await self._archive(room)
            except Exception:
                logger.exception('Painted Maze terminal refresh failed: %s', room['id'])

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
