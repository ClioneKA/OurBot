"""Discord-facing private-thread service for the Painted Maze roguelite."""
import asyncio
from dataclasses import asdict
import logging
import time

import discord
from discord.ext import tasks

from core.rpg_character import CharacterError, ITEMS
from core.rpg_crystals import CrystalStore
from core.rpg_painted_maze import (
    COLOR_CONTRACTS, ENTRY_CLOSED_NOTICE, ENTRY_ENABLED, MODE_NAME,
    PaintedMazeError, PaintedMazeStore,
)
from core.rpg_painted_maze_battle import simulate_final_battle, simulate_room_painting
from core.rpg_painted_maze_rewards import PaintedMazeRewardStore


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
        self.advance.label = '挑戰最終畫室' if final else '討伐下一幅畫作'

    @discord.ui.button(label='討伐下一幅畫作', style=discord.ButtonStyle.danger,
                       custom_id='painted_maze:progress:advance')
    async def advance(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            room = await self.service.advance(self.room_id, interaction.user)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            '戰鬥已結算。' if room['status'] not in ('completed', 'failed')
            else f'迷廊已結束：{room.get("final_battle", {}).get("result", room.get("end_reason", "已結束"))}。',
            ephemeral=True)


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
                view = (ContractVoteView(self, room) if room['status'] == 'contract'
                        else MazeProgressView(self, room['id'], final=room.get('boss_index') == 9))
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
        if not ENTRY_ENABLED:
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
        if not leave and not ENTRY_ENABLED:
            raise PaintedMazeError(ENTRY_CLOSED_NOTICE)
        if member.bot:
            raise PaintedMazeError('機器人不能進入繪境迷廊。')
        async with self.lock(room_id):
            room = self.repo.get(room_id)
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
        if not ENTRY_ENABLED:
            raise PaintedMazeError(ENTRY_CLOSED_NOTICE)
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or member.id != room['host_id']:
                raise PaintedMazeError('只有房主可以開始繪境迷廊。')
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

    async def advance(self, room_id, member):
        async with self.lock(room_id):
            room = self.repo.get(room_id)
            if not room or member.id not in room.get('members', ()):
                raise PaintedMazeError('你不在這個繪境迷廊隊伍中。')
            if room['status'] != 'running':
                raise PaintedMazeError('目前不能推進繪境迷廊。')
            if room['boss_index'] < 9:
                result = simulate_room_painting(room)
                room = self.repo.settle_painting(
                    room_id, member.id, room['boss_index'], result['result'],
                    result['battle'], result['party_state'])
            else:
                result = simulate_final_battle(room)
                room = self.repo.settle_final(
                    room_id, member.id, result['result'], result['battle'], result['party_state'])
            room = self.recover_rewards(room_id)
            await self._post_battle_report(room)
            await self._refresh(room)
            if room['status'] in ('completed', 'failed'):
                self.cog.divinations.clear_raid(room_id)
                await self._archive(room)
            return room

    async def _post_battle_report(self, room):
        thread = await self._thread(room)
        battle = room.get('last_battle') or {}
        if not thread or not battle:
            return
        if room.get('final_battle') and room['final_battle'].get('completed_at'):
            title = '最終畫室戰報'
        else:
            history = room.get('battle_history', ())
            title = f'第 {len(history)} 幅畫作戰報'
        fighters = battle.get('fighters', ())
        roster = []
        for fighter in (item for item in fighters if item.get('team') == 0):
            maximum = fighter.get('stats', {}).get('HP', 0)
            status = '💀 倒下' if fighter.get('hp', 0) <= 0 else '存活'
            mention = f'<@{fighter.get("user_id")}>' if fighter.get('user_id') else fighter.get('name')
            roster.append(f'{mention}：{fighter.get("hp", 0):,}/{maximum:,} HP｜{status}')
        logs = list(battle.get('log', ())) or ['無戰鬥記錄']
        checkpoint = None
        if room.get('final_battle') and room['final_battle'].get('result') == '勝利':
            checkpoint = 4
        elif (room.get('battle_history') and room['battle_history'][-1].get('result') == '勝利'
              and (room['battle_history'][-1]['painting_index'] + 1) % 3 == 0):
            checkpoint = (room['battle_history'][-1]['painting_index'] + 1) // 3
        reward_lines = []
        if checkpoint:
            currency = self.rewards.currency_rewards(room['id'], checkpoint)
            reward_lines.extend(
                f'<@{reward["user_id"]}>：{reward["xp"]:,} XP、{reward["gold"]:,} 金幣'
                for reward in currency)
            marker = next((item for item in reversed(room.get('sealed_rewards', ()))
                           if (checkpoint <= 3 and item.get('kind') == 'crystals'
                               and item.get('stage') == checkpoint)
                           or (checkpoint == 4 and item.get('kind') == 'shadow_bonus')), None)
            if marker:
                crystals = [self.crystals.get(instance_id)
                            for instance_id in marker.get('instance_ids', ())]
                crystal_counts = {}
                for crystal in filter(None, crystals):
                    crystal_counts[crystal.user_id] = crystal_counts.get(crystal.user_id, 0) + 1
                reward_lines.extend(f'<@{user_id}>：顏料結晶 ×{count}'
                                    for user_id, count in crystal_counts.items())
            if checkpoint == 4 and room.get('route') == 'noah':
                for reward in self.rewards.rewards(room['id']):
                    text = (f'獲得【{ITEMS[reward["item_id"]].name}】，可從背包使用'
                            if reward['status'] == 'box_granted'
                            else f'獲得【{ITEMS[reward["item_id"]].name}】')
                    reward_lines.append(f'<@{reward["user_id"]}>：{text}')
        chunks, current = [], ''
        for line in logs:
            line = str(line)
            if len(current) + len(line) + 1 > 3600:
                chunks.append(current)
                current = line
            else:
                current = f'{current}\n{line}'.strip()
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title=title if index == 1 else f'{title}（{index}）',
                description=chunk, color=0xDC2626)
            if index == 1:
                embed.add_field(name='隊伍結果', value='\n'.join(roster)[:1024] or '無', inline=False)
                if reward_lines:
                    embed.add_field(name='本次獎勵', value='\n'.join(reward_lines)[:1024], inline=False)
                embed.set_footer(text=f'{battle.get("result", "已結算")}｜{battle.get("round", 0)} 回合')
            await thread.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def recover_rewards(self, room_id):
        """Drain the room's transactional outbox; every individual seal is idempotent."""
        room = self.repo.get(room_id)
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
        if room['status'] == 'contract':
            view = ContractVoteView(self, room)
        elif room['status'] == 'running':
            view = MazeProgressView(self, room['id'], final=room['boss_index'] == 9)
        else:
            view = None
        await thread.get_partial_message(room['message_id']).edit(
            embed=self.room_embed(room), view=view,
            allowed_mentions=discord.AllowedMentions.none())

    async def _archive(self, room):
        thread = await self._thread(room)
        if thread and not thread.archived:
            await thread.edit(archived=True, locked=True, reason='繪境迷廊已結束')

    def lobby_embed(self, room):
        route = '繪畫魔女．城崎諾亞' if room['route'] == 'noah' else '繪畫之影'
        state = {
            'lobby': '等待中', 'running': '進行中', 'contract': '契約投票中',
            'completed': '已完成', 'failed': '挑戰失敗', 'expired': '已逾期',
            'cancelled': '已關閉', 'admin_ended': '管理員已結束',
        }.get(room['status'], room['status'])
        return discord.Embed(
            title=f'{MODE_NAME} #{room["number"]}｜{state}', color=0xA855F7,
            description=(f'房主：<@{room["host_id"]}>\n路線：**{route}**\n'
                         f'入場：{"開始時消耗畫作" if room.get("requires_entry", True) else "管理員測試（免畫作）"}\n'
                         f'隊伍：{len(room["members"])}/8\n'
                         + '\n'.join(f'• <@{uid}>' for uid in room['members'])))

    def room_embed(self, room):
        embed = discord.Embed(title=f'{MODE_NAME} #{room["number"]}', color=0x7C3AED)
        if room['status'] == 'lobby':
            embed.description = '這是私人作戰討論串。隊員可在此調整戰術，由房主在大廳面板開始。'
            embed.add_field(name='保留時間', value=f'<t:{int(room["expires_at"])}:R>')
            return embed
        if room.get('last_battle'):
            embed.add_field(name='上一戰', value=(
                f'{room["last_battle"].get("result", room.get("final_battle", {}).get("result", "已結算"))}'
                f'｜{room["last_battle"].get("round", 0)} 回合'), inline=False)
        embed.add_field(name='畫作進度', value=f'{room["boss_index"]}/9', inline=True)
        contracts = room.get('contracts', ())
        embed.add_field(name='色彩契約', value='\n'.join(
            COLOR_CONTRACTS[key]['name'] for key in contracts) or '尚未選擇', inline=True)
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
        if room['status'] == 'contract':
            vote = room['contract_vote']
            lines = [f'**{COLOR_CONTRACTS[key]["name"]}**\n'
                     f'隊伍：{COLOR_CONTRACTS[key]["party"]}\n'
                     f'反噬：{COLOR_CONTRACTS[key]["backlash"]}' for key in vote['candidates']]
            embed.add_field(name=f'第 {vote["round"]} 次契約投票', value='\n\n'.join(lines), inline=False)
            embed.add_field(name='截止', value=f'<t:{int(vote["deadline"])}:R>')
        elif room['status'] == 'running':
            if room['boss_index'] < 9:
                stage_start = room['boss_index'] // 3 * 3
                lines = []
                for index in range(stage_start, stage_start + 3):
                    painting = room['paintings'][index]
                    marker = '✅' if index < room['boss_index'] else ('➡️' if index == room['boss_index'] else '⬜')
                    lines.append(f'{marker} {painting["name"]}｜T{painting["tier"]}')
                embed.add_field(name=f'第 {stage_start // 3 + 1} 幕路線',
                                value='\n'.join(lines), inline=False)
            else:
                boss = ('繪畫魔女．城崎諾亞'
                        if room['route'] == 'noah' else '繪畫之影')
                embed.add_field(name='最終畫室', value=boss, inline=False)
        else:
            embed.add_field(name='結果', value=room.get('end_reason', room['status']), inline=False)
            if room['status'] == 'completed' and room['route'] == 'noah':
                embed.add_field(name='菁英裝備', value=(
                    '首次通關者會取得本職菁英裝備自選箱，可從背包使用；'
                    '已有通關紀錄者已隨機發放。'), inline=False)
        embed.set_footer(text='房間最長保留 24 小時；結束後討論串會封存但隊員仍可查看。')
        return embed

    @tasks.loop(seconds=5)
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
                self.cog.divinations.clear_raid(room['id'])
                await self._refresh(room)
                await self._archive(room)
            except discord.HTTPException:
                logger.exception('Painted Maze expiry refresh failed: %s', room['id'])

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
