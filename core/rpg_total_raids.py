"""Discord room lifecycle and interaction views for manual total raids."""
import asyncio
from dataclasses import asdict
import json
import io
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks

from core.rpg_battle import PASSIVES, rule_skill
from core.rpg_character import CharacterError
from core.rpg_character import add_owned_item
from core.rpg_witch_catalog import WITCH_BOSS, IDS, PROFILE
from core.rpg_witch_battle import WitchRaidBattle, witch_battle_from_participants, ACTION_DEFEND, SPELL_DESCRIPTIONS, WITCH_TRAITS
from core.rpg_expeditions import is_expedition_active, require_not_expedition
from core.rpg_raids import channel_ids
from core.rpg_total_battle import (
    ACTION_ATTACK,
    ACTION_CANVAS,
    ACTION_SKILL,
    NOAH_JOB,
    PAINT_EFFECTS,
    PAINT_NAMES,
    TotalRaidError,
    dump_total_battle,
    load_total_battle,
    noah_total_battle_from_participants,
    training_dummy_battle_from_participants,
)


logger = logging.getLogger(__name__)
TOTAL_RAID_BOSSES = ('訓練用假人', NOAH_JOB)
PRIVATE_CONFIRMATION_SECONDS = 8
WITCH_ROUND_SECONDS = 120


def witch_day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,
                                  timezone(timedelta(hours=8))).date().isoformat()


def hp_bar(current, maximum, width=16):
    """Compact text progress bar that still shows a sliver above zero HP."""
    if maximum <= 0:
        filled = 0
        percent = 0
    else:
        ratio = max(0.0, min(1.0, current / maximum))
        filled = round(ratio * width)
        if current > 0:
            filled = max(1, filled)
        percent = current * 100 / maximum
    return f'`{"█" * filled}{"░" * (width - filled)}` {percent:.1f}%'


def field_chunks(lines, limit=1024):
    """Split complete log lines into Discord fields without dropping text."""
    chunks = []
    current = ''
    for original in lines:
        parts = [original[index:index + limit] for index in range(0, len(original), limit)] or ['']
        for part in parts:
            candidate = f'{current}\n{part}' if current else part
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    return chunks


def effect_status(fighter, battle):
    """Return concise buffs/debuffs that will affect the upcoming action."""
    turn = battle.planning_round if not battle.result else battle.round

    def remaining(effect):
        return fighter.effects[effect] - turn + 1

    buffs, debuffs = [], []
    if isinstance(battle, WitchRaidBattle):
        if fighter.job in battle.ids:
            phase = battle.phase(fighter)
            buffs.append(f'魔女化（{phase} 階）')
            if fighter.job == 'hanna' and (turn % 2 or fighter.has('float', turn)):
                buffs.append('浮游：裝甲步兵／騎士非必中攻擊減傷 30%')
            if fighter.job == 'milia' and battle.adaptation and battle.adaptation[0] == turn:
                kind = {'basic': '普攻', 'skill': '單體技能', 'area': '全體技能'}.get(battle.adaptation[1], '無')
                buffs.append(f'適應：{kind}傷害減半')
        for key, label in (('brainwash', '洗腦（不可淨化）'), ('factor', '魔女因子'), ('burn', '火傷'),
                           ('vision', '預知：避免重複行動'), ('doubt', '懷疑'), ('watch', '監視'),
                           ('exchange', '交換標記'), ('no_look', '無法指定召喚畫')):
            if fighter.has(key, turn):
                debuffs.append(label)
                if key == 'factor':
                    debuffs[-1] += f' {fighter.status_stacks.get(key, 0)}/3 層'
        if fighter.team == 0 and battle.forced(fighter, turn):
            debuffs.append('強制普攻隊友')
        for key, label in (('spotlight', '聚光：承接單體攻擊'), ('snake_guard', '白蛇守護'),
                           ('float', '浮游'), ('defend', '防禦')):
            if fighter.has(key, turn):
                buffs.append(label)
        shield, until = battle.prayer_shields.get(battle.key(fighter), (0, -1))
        if shield and until >= turn:
            buffs.append(f'祈禱護盾 {shield} HP')
    if fighter.passive_id and fighter.job in PASSIVES:
        passive = next((item for item in PASSIVES[fighter.job] if item.id == fighter.passive_id), None)
        if passive:
            state = fighter.passive_state
            counters = {
                ('裝甲步兵', 1): f'連式{state.get("chain", 0)}/3',
                ('裝甲步兵', 2): f'攻勢{state.get("offense", 0)}/2・守勢{state.get("guard_stance", 0)}/2',
                ('裝甲步兵', 3): f'血怒{state.get("blood_rage", 0)}/5',
                ('騎士', 1): f'復仇{state.get("revenge", 0)}/3',
                ('騎士', 2): f'守望{state.get("watch", 0)}/2',
                ('騎士', 3): {'opening': '破綻', 'momentum': '衝勢'}.get(state.get('lance_combo'), '待機'),
                ('弓兵', 1): f'箭勢{state.get("arrow_tempo", 0)}/6',
                ('弓兵', 3): f'洞察{state.get("insight", 0)}/3',
                ('僧侶', 1): f'恩典{state.get("grace", 0)}/3',
                ('僧侶', 2): f'樂章{len(state.get("hymn_verses", []))}/3',
                ('僧侶', 3): f'輝光{state.get("radiance", 0)}/3・戒律{state.get("discipline", 0)}/3',
            }
            detail = counters.get((fighter.job, fighter.passive_id))
            buffs.append(f'{passive.name}({detail})' if detail else passive.name)
    if fighter.has('guard', turn):
        buffs.append(f'護衛(防禦+{fighter.guard_bonus}・負面免疫・{remaining("guard")}回合)')
    if fighter.has('bless', turn):
        buffs.append(f'祝福(攻擊+25%・{remaining("bless")}回合)')
    if fighter.has('stance', turn):
        reduction = {'民兵': 20, '騎士': 50}.get(fighter.job, 35)
        buffs.append(f'防禦姿態(減傷{reduction}%・{remaining("stance")}回合)')
    if fighter.has('taunt', turn):
        buffs.append(f'挑釁反擊(反擊100%・{remaining("taunt")}回合)')
    if fighter.has('moon_shadow', turn):
        buffs.append(f'月影(閃避+15%・{remaining("moon_shadow")}回合)')
    if fighter.food_regen_left and turn >= fighter.food_regen_start:
        buffs.append(f'{fighter.food_name}緩補({fighter.food_regen_left}回合)')
    if fighter.has('break', turn):
        effect = '防禦-25%' if isinstance(battle, WitchRaidBattle) else '防禦歸零'
        debuffs.append(f'破甲({effect}・{remaining("break")}回合)')
    if fighter.has('poison', turn):
        debuffs.append(f'中毒({remaining("poison")}回合)')
    if fighter.has('stun', turn):
        debuffs.append(f'暈眩({remaining("stun")}回合)')
    if fighter.has('weak', turn):
        debuffs.append(f'虛弱(攻擊-20%・{remaining("weak")}回合)')
    poison_arrows = fighter.status_stacks.get('poison_arrows', [])
    if poison_arrows:
        debuffs.append(f'毒箭侵蝕({len(poison_arrows)}支)')
    toxicity = fighter.status_stacks.get('passive_toxicity', {})
    if toxicity and max(toxicity.values(), default=0):
        debuffs.append(f'毒性(最高{max(toxicity.values())}/3層)')
    if fighter.has('vulnerable', turn):
        debuffs.append(f'易傷(+10%・{remaining("vulnerable")}回合)')
    corruption = fighter.status_stacks.get('corruption', 0)
    if corruption:
        debuffs.append(f'腐敗({corruption}/3層)')
    paint = fighter.status_stacks.get('paint_mask', 0)
    if paint:
        buffs.append(f'顏料・{PAINT_NAMES.get(paint, "未知")}({PAINT_EFFECTS.get(paint, "")})')
    if fighter.status_stacks.get('source_erosion'):
        debuffs.append('源色侵蝕(本回合命中即抹除)')
    if fighter.job == NOAH_JOB:
        source = battle.mechanics.get('noah_source_stacks', 0)
        if source:
            buffs.append(f'源色({source}層・攻擊+{source * 10}%)')
    return ('、'.join(f'**{item}**' for item in buffs) or '無',
            '、'.join(f'**{item}**' for item in debuffs) or '無')


def witch_transformation_notes(battle):
    notes = []
    for fighter in battle.fighters:
        if fighter.team != 1 or fighter.job not in battle.ids:
            continue
        phase = battle.phase(fighter)
        ability = SPELL_DESCRIPTIONS[fighter.job][phase]
        if fighter.job == 'hiro' and battle.rewound:
            ability = ('單體兩段打擊' if phase == 2 else '單體打擊') + '；本場回溯已使用'
        bonus = f'傷害與治療 +{phase * 10}%' if phase else '尚無魔女化傷害／治療加成'
        notes.append(f'**{fighter.name}｜魔女化 {phase} 階**\n{ability}；{bonus}。')
    return notes


def last_round_log(battle):
    saved = battle.mechanics.get('last_round_log')
    if saved is not None:
        return list(saved)
    if not battle.round:
        return ['尚未結算任何回合。']
    marker = f'── 第 {battle.round} 回合 ──'
    try:
        start = len(battle.log) - 1 - battle.log[::-1].index(marker)
    except ValueError:
        start = 0
    return battle.log[start:] or ['該回合沒有產生紀錄。']


def active_effect_notes(battle):
    """Explain only effects actually displayed, once each for the whole party."""
    turn = battle.planning_round if not battle.result else battle.round
    descriptions = {
        'factor': '魔女因子：最多 3 層；艾瑪引爆時每層提高傷害，引爆後清空。可淨化；預告後不因層數變動改換目標。',
        'burn': '火傷：回合結束扣除最大 HP 的 3%；可淨化，會影響亞里沙的擴散與引爆。',
        'brainwash': '洗腦：一般階段傷害轉向隊友、治療轉向魔女；一次／二次魔女化改為強制普攻隊友。不可淨化，打斷安安或使她倒下可解除。',
        'vision': '預知：重複上一回合普攻或同名技能時，本次攻擊力降低 40%；奈葉香魔女化後另追加攻擊。',
        'doubt': '懷疑：傷害與治療降低 30%，行動後解除；防禦可避免魔法施加。',
        'watch': '監視：可可二次魔女化的打擊對被監視者追加傷害，打擊後消耗標記。',
        'exchange': '交換標記：兩名預告對象交換 HP 比例；一次魔女化也交換火傷、破甲、虛弱、祝福。淨化任一標記可阻止交換。',
        'no_look': '無法指定召喚畫：不能直接指定動物召喚畫為目標，全體攻擊仍可波及。',
        'spotlight': '聚光：承接玩家原本指定其他魔女的單體攻擊；魔女化後減傷 25%，結束後遭破甲。',
        'snake_guard': '白蛇守護：白蛇存活期間，受保護魔女受到的傷害降低 25%。',
        'float': '浮游：漢娜承受裝甲步兵與騎士的非必中攻擊時減傷 30%。',
        'defend': '防禦：本回合承受傷害減半。',
        'guard': '護衛：提高防禦；負面免疫依護衛剩餘效果生效，無法阻擋洗腦。',
        'bless': '祝福：攻擊力提高 25%。',
        'stance': '防禦姿態：依職業降低承受傷害，比例見角色增益列。',
        'taunt': '挑釁反擊：吸引尚未鎖定目標的攻擊並反擊；已預告的魔女指定技能不再轉向。',
        'moon_shadow': '月影：閃避提高 15 個百分點。',
        'break': '破甲：在魔女試煉中，受攻擊時防禦降低 25%。',
        'poison': '中毒：行動前扣血，玩家為最大 HP 的 5%，敵方為 2%；可淨化。',
        'stun': '暈眩：無法行動；對魔女施加時也會打斷正在準備的魔法與連動。',
        'weak': '虛弱：攻擊力降低 20%。',
        'vulnerable': '易傷：受到傷害提高 10%。',
    }
    notes = {}
    for fighter in battle.fighters:
        if fighter.hp <= 0:
            continue
        for key, description in descriptions.items():
            if fighter.has(key, turn):
                notes[key] = description
        for passive in PASSIVES.get(fighter.job, ()):
            if passive.id == fighter.passive_id:
                notes[f'passive:{fighter.job}:{passive.id}'] = f'{passive.name}：{passive.description}'
        if fighter.status_stacks.get('poison_arrows'):
            notes['poison_arrows'] = '毒箭侵蝕：每支毒箭獨立計算持續傷害與期限，可淨化。'
        if fighter.status_stacks.get('passive_toxicity'):
            notes['toxicity'] = '毒性：每位施毒者各最多 3 層；猛毒調律直接命中滿層目標時可引爆。'
        if fighter.status_stacks.get('corruption'):
            notes['corruption'] = '腐敗：滿 3 層時於行動前爆裂，自身扣除最大 HP 的 12%，其他隊友扣除最大 HP 的 3%，然後清空。'
        if fighter.food_regen_left and turn >= fighter.food_regen_start:
            notes[f'food:{fighter.food_name}'] = f'{fighter.food_name}緩補：每回合恢復最大 HP 的 {fighter.food_regen_permille / 10:g}%。'
        shield, until = battle.prayer_shields.get(battle.key(fighter), (0, -1))
        if shield and until >= turn:
            notes['prayer'] = '祈禱護盾：梅露露的溢出治療轉為護盾，先吸收傷害；數值見魔女增益列。'
    return list(notes.values())


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
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_witch_days (day TEXT PRIMARY KEY, ids TEXT NOT NULL)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_announcements (
                channel_id INTEGER PRIMARY KEY, day TEXT NOT NULL, message_id INTEGER NOT NULL)''')
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_witch_rewards (room_id TEXT PRIMARY KEY)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_channels (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)''')

    def daily_witches(self, now=None):
        day = witch_day(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT ids FROM rpg_witch_days WHERE day=?', (day,)).fetchone()
            if row:
                return day, json.loads(row[0])
            ids = random.SystemRandom().sample(IDS, 3)
            self.db.execute('INSERT INTO rpg_witch_days VALUES (?,?)', (day, json.dumps(ids)))
        return day, ids

    def start_witch(self, room):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.get(room['id'])
            if not saved or saved['status'] != 'lobby':
                raise TotalRaidError('這個房間已經開始或關閉。')
            for uid in room['members']:
                require_not_expedition(self.db, uid)
            self.db.execute('UPDATE rpg_total_raids SET status=?,data=? WHERE id=?',
                (room['status'], json.dumps(room, ensure_ascii=False), room['id']))

    def finish_witch(self, room, battle):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            claimed = self.db.execute('INSERT OR IGNORE INTO rpg_witch_rewards VALUES (?)', (room['id'],))
            if claimed.rowcount and battle.result == '勝利':
                for uid in room['members']:
                    add_owned_item(self.db, room['guild_id'], uid, 'witch:thread', 1)
            room['reward_text'] = '每位參戰者獲得 1 個魔女繡線。' if battle.result == '勝利' else '本次未勝，沒有繡線報酬。'
            room['public_pending'] = True
            self.db.execute('UPDATE rpg_total_raids SET status=?,data=? WHERE id=?',
                (room['status'], json.dumps(room, ensure_ascii=False), room['id']))

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
            self.db.execute('BEGIN IMMEDIATE')
            if is_expedition_active(self.db, host_id):
                raise TotalRaidError('你正在遠征，請等待返回或先中斷遠征。')
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
            self.db.execute('BEGIN IMMEDIATE')
            if room['status'] in ('lobby', 'running'):
                if any(is_expedition_active(self.db, user) for user in room['members']):
                    raise TotalRaidError('隊員正在遠征，請等待返回或先中斷遠征。')
            self.db.execute('UPDATE rpg_total_raids SET status=?, data=? WHERE id=?',
                            (room['status'], json.dumps(room, ensure_ascii=False), room['id']))


class WitchDailyView(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(label='開啟魔女試煉', style=discord.ButtonStyle.danger,
                       custom_id='witch_raid:daily:open')
    async def open_room(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.guild is None or not self.service.cog.store.has_player(interaction.guild_id, interaction.user.id):
                raise TotalRaidError('請先接受邀請函，正式成為冒險者。')
            room, channel = await self.service.create_room(interaction.guild, interaction.user, WITCH_BOSS)
            await interaction.followup.send(f'已開啟當日魔女房間：{channel.mention}', ephemeral=True)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException:
            logger.exception('Witch room creation failed')
            await interaction.followup.send('無法建立房間，請確認機器人具有管理頻道權限。', ephemeral=True)


class TotalRaidLobbyView(discord.ui.View):
    def __init__(self, service, room_id):
        super().__init__(timeout=None)
        self.service, self.room_id = service, room_id

    async def _change(self, interaction, leave=False):
        try:
            if not self.service.cog.store.has_player(interaction.guild_id, interaction.user.id):
                raise TotalRaidError('請先接受邀請函，正式成為冒險者。')
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
        room = self.service.repo.get(self.room_id)
        if room and room['boss'] == WITCH_BOSS:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.service.cancel_lobby(self.room_id, interaction.user)
                await self.service.cleanup_witch_rooms(time.time())
                await interaction.followup.send('房間已關閉；紀錄送達魔女試煉頻道後會刪除房間。', ephemeral=True)
            except (CharacterError, TotalRaidError) as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            return
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
        room = service.repo.get(room_id)
        if not room or room['boss'] != WITCH_BOSS:
            self.remove_item(self.confirm)
            self.remove_item(self.takeover)

    @discord.ui.button(label='確認本回合', style=discord.ButtonStyle.success,
                       custom_id='witch_raid:running:confirm')
    async def confirm(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.service.confirm_action(self.room_id, interaction.user.id)
            await interaction.followup.send('已確認本回合；全員確認後結算。', ephemeral=True)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @discord.ui.button(label='接管自動普攻', style=discord.ButtonStyle.secondary,
                       custom_id='witch_raid:running:takeover')
    async def takeover(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.service.takeover_action(self.room_id, interaction.user.id)
            await interaction.followup.send('已接管，請選擇並確認本回合行動。', ephemeral=True)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

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
            self.service.player_action_text(battle, interaction.user.id),
            view=view, ephemeral=True,
        )


class WitchPublicSelect(discord.ui.Select):
    def __init__(self, parent, battle, target=False):
        self.parent_view, self.target = parent, target
        self.round = battle.planning_round
        if target:
            options = [discord.SelectOption(label=f.name[:100], value=battle.key(f),
                       description=f'{"隊友" if f.team == 0 else "敵方"} HP {f.hp:,}')
                       for f in battle.fighters if f.hp > 0][:25]
        else:
            options = [discord.SelectOption(label=label, value=value) for label, value in
                       [('普通攻擊', ACTION_ATTACK), ('防禦', ACTION_DEFEND),
                        ('技能槽 1', 'skill:1'), ('技能槽 2', 'skill:2'), ('技能槽 3', 'skill:3')]]
        super().__init__(placeholder='② 選擇自己的行動目標' if target else '① 選擇自己的行動（技能名稱見隊伍狀態）',
                         options=options, row=2 if target else 1,
                         custom_id=f'witch_raid:public:{"target" if target else "action"}:{self.round}')

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.parent_view.service.public_choice(
                self.parent_view.room_id, interaction.user.id, self.round, self.values[0], self.target)
            await interaction.followup.send(message, ephemeral=True)
        except (CharacterError, TotalRaidError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


class WitchBattleView(TotalRaidRunningView):
    def __init__(self, service, room_id):
        super().__init__(service, room_id)
        self.remove_item(self.choose)
        battle = load_total_battle(service.repo.get(room_id)['battle'])
        self.add_item(WitchPublicSelect(self, battle))
        self.add_item(WitchPublicSelect(self, battle, target=True))

    async def page(self, interaction, delta, status=False):
        await interaction.response.defer()
        async with self.service.lock(self.room_id):
            room = self.service.repo.get(self.room_id)
            if room['status'] != 'running':
                return
            battle = load_total_battle(room['battle'])
            self.service.battle_embed(room, battle)
            count = room.get('status_page_count', 1) if status else len(field_chunks(last_round_log(battle), 900)) or 1
            key = 'status_page' if status else 'log_page'
            room[key] = (room.get(key, 0) + delta) % count
            self.service.repo.save(room)
            await self.service._edit_public(room, battle)

    @discord.ui.button(label='摘要上一頁', custom_id='witch_raid:log:previous', row=3)
    async def previous(self, interaction, _button):
        await self.page(interaction, -1)

    @discord.ui.button(label='摘要下一頁', custom_id='witch_raid:log:next', row=3)
    async def next_page(self, interaction, _button):
        await self.page(interaction, 1)

    @discord.ui.button(label='狀態／說明上一頁', custom_id='witch_raid:status:previous', row=3)
    async def previous_status(self, interaction, _button):
        await self.page(interaction, -1, status=True)

    @discord.ui.button(label='狀態／說明下一頁', custom_id='witch_raid:status:next', row=3)
    async def next_status(self, interaction, _button):
        await self.page(interaction, 1, status=True)


class TotalRaidActionSelect(discord.ui.Select):
    def __init__(self, parent, battle):
        options = []
        for action in battle.available_actions(parent.user_id):
            remaining = action.get('cooldown_remaining', 0)
            if remaining:
                continue
            if action['action'] in (ACTION_ATTACK, ACTION_DEFEND):
                value = action['action']
            elif action['action'] == ACTION_CANVAS:
                value = f'{ACTION_CANVAS}:{action["canvas_color"]}'
            else:
                value = f'{ACTION_SKILL}:{action["skill_slot"]}'
            options.append(discord.SelectOption(
                label=action['name'][:100], value=value,
                description=action['description'][:100],
            ))
        super().__init__(placeholder='選擇普通攻擊或技能', options=options,
                         custom_id='total_raid:private:action')
        self.parent_view = parent

    async def callback(self, interaction):
        raw = self.values[0]
        action, detail = (raw.split(':', 1) + [None])[:2] if ':' in raw else (raw, None)
        slot = int(detail) if action == ACTION_SKILL else None
        special_target = detail if action == ACTION_CANVAS else None
        try:
            _room, battle = self.parent_view.service.running_battle(
                self.parent_view.room_id, interaction.user.id)
            if battle.planning_round != self.parent_view.planning_round:
                raise TotalRaidError('此面板的回合已結束，請重新開啟行動面板。')
            targets = battle.valid_targets(interaction.user.id, action, slot)
            if not targets:
                await interaction.response.defer()
                if action == ACTION_CANVAS:
                    await self.parent_view.service.submit_action(
                        self.parent_view.room_id, interaction.user.id, action, special_target, slot,
                        expected_round=self.parent_view.planning_round)
                else:
                    await self.parent_view.service.submit_action(
                        self.parent_view.room_id, interaction.user.id, action, None, slot,
                        expected_round=self.parent_view.planning_round)
                await interaction.edit_original_response(
                    content='本回合行動已登記；結算前仍可重新選擇。', view=None)
                self.parent_view.service.schedule_private_cleanup(interaction)
                return
            view = TotalRaidTargetView(
                self.parent_view.service, self.parent_view.room_id,
                interaction.user.id, action, slot, battle, targets,
            )
        except (CharacterError, TotalRaidError) as exc:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=str(exc), view=None)
            else:
                await interaction.response.edit_message(content=str(exc), view=None)
            return
        await interaction.response.edit_message(content='請選擇目標。', view=view)


class TotalRaidActionChoiceView(discord.ui.View):
    def __init__(self, service, room_id, user_id, battle):
        super().__init__(timeout=120)
        self.service, self.room_id, self.user_id = service, room_id, user_id
        self.planning_round = battle.planning_round
        self.add_item(TotalRaidActionSelect(self, battle))
        actor = next(fighter for fighter in battle.living(0) if fighter.user_id == user_id)
        if battle.noah_phase() == 2 and battle.paint_mask(actor) and len(battle.living(0)) > 1:
            self.add_item(TotalRaidPaintGiftSelect(self, battle))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message('這不是你的行動面板。', ephemeral=True)
        return False


class TotalRaidPaintGiftSelect(discord.ui.Select):
    def __init__(self, parent, battle):
        actor = next(fighter for fighter in battle.living(0) if fighter.user_id == parent.user_id)
        options = [discord.SelectOption(
            label=fighter.name[:100], value=battle.key(fighter),
            description=f'將你的{PAINT_NAMES[battle.paint_mask(actor)]}顏料給予此隊友'[:100],
        ) for fighter in battle.living(0) if fighter is not actor]
        super().__init__(placeholder='額外操作：給予顏料', options=options,
                         custom_id='total_raid:private:paint_gift', row=1)
        self.parent_view = parent

    async def callback(self, interaction):
        await interaction.response.defer()
        try:
            await self.parent_view.service.submit_paint_gift(
                self.parent_view.room_id, interaction.user.id, self.values[0])
        except (CharacterError, TotalRaidError) as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        await interaction.edit_original_response(
            content='顏料給予已登記；這不會消耗本回合的普通攻擊或技能行動。', view=None)
        self.parent_view.service.schedule_private_cleanup(interaction)


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
                expected_round=self.parent_view.planning_round,
            )
        except (CharacterError, TotalRaidError) as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        await interaction.edit_original_response(
            content='本回合行動已登記；結算前仍可重新選擇。', view=None)
        self.parent_view.service.schedule_private_cleanup(interaction)


class TotalRaidTargetView(discord.ui.View):
    def __init__(self, service, room_id, user_id, action, skill_slot, battle, targets):
        super().__init__(timeout=120)
        self.service, self.room_id, self.user_id = service, room_id, user_id
        self.planning_round = battle.planning_round
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
        self.witch_channel_ids = channel_ids(os.getenv('RPG_WITCH_RAID_CHANNEL_IDS', ''), 'RPG_WITCH_RAID_CHANNEL_IDS')
        self.repo = TotalRaidStore(cog.store)
        self.locks = {}
        self.views = {}
        self.private_cleanup_tasks = set()
        self.witch_channel_retry_at = {}
        self.refreshed_announcements = set()

    def start(self):
        self.bot.add_view(WitchDailyView(self))
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
        pending = list(self.private_cleanup_tasks)
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def view(self, room):
        key = (room['id'], room['status'])
        if room['boss'] == WITCH_BOSS and room['status'] == 'running':
            old = self.views.pop(key, None)
            if old:
                old.stop()
            self.views[key] = WitchBattleView(self, room['id'])
        if key not in self.views:
            cls = TotalRaidLobbyView if room['status'] == 'lobby' else TotalRaidRunningView
            self.views[key] = cls(self, room['id'])
        return self.views[key]

    def lock(self, room_id):
        return self.locks.setdefault(room_id, asyncio.Lock())

    def schedule_private_cleanup(self, interaction, delay=PRIVATE_CONFIRMATION_SECONDS):
        task = asyncio.create_task(self._delete_private_response(interaction, delay))
        self.private_cleanup_tasks.add(task)
        task.add_done_callback(self.private_cleanup_tasks.discard)

    @staticmethod
    async def _delete_private_response(interaction, delay):
        try:
            await asyncio.sleep(delay)
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    def category_for(self, guild):
        for category_id in sorted(self.category_ids):
            category = guild.get_channel(category_id)
            if isinstance(category, discord.CategoryChannel):
                return category
        raise CharacterError('這個伺服器尚未設定總力戰類別。')

    async def create_room(self, guild, host, boss):
        async with self.lock(('guild', guild.id)):
            return await self._create_room(guild, host, boss)

    async def _create_room(self, guild, host, boss):
        require_not_expedition(self.repo.db, host.id)
        if not self.settings.enabled:
            raise CharacterError('總力戰目前未開放。')
        if boss not in (*TOTAL_RAID_BOSSES, WITCH_BOSS):
            raise CharacterError('尚未支援這個總力戰 Boss。')
        if any(active['guild_id'] == guild.id and host.id in active['members']
               for active in self.repo.active()):
            raise CharacterError('你已在這個伺服器的另一個總力戰房間中。')
        category = self.category_for(guild)
        number = self.repo.reserve_number(guild.id, boss)
        name = f'魔女試煉-{number}' if boss == WITCH_BOSS else f'總力戰-{boss}-{number}'
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
            topic=f'{"魔女試煉" if boss == WITCH_BOSS else "總力戰測試房"}｜房主：{host}｜Boss：{boss}',
            reason=f'{host} 建立總力戰房間',
        )
        try:
            room = self.repo.create(guild.id, category.id, channel.id, host.id, boss, number)
        except TotalRaidError:
            await channel.delete(reason='房主已開始遠征，取消建立總力戰房間')
            raise
        if boss == WITCH_BOSS:
            room['witch_day'], room['witch_ids'] = self.repo.daily_witches()
            self.repo.save(room)
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
        room = self.repo.get(room_id)
        if not room:
            raise TotalRaidError('房間已關閉。')
        async with self.lock(('guild', room['guild_id'])):
            return await self._change_member(room_id, member, leave)

    async def _change_member(self, room_id, member, leave=False):
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
                passive = self.cog.tactics.passive(room['guild_id'], user_id, state['job'])
                participants.append(dict(
                    id=user_id, name=participant.display_name[:16], state=state,
                    rules=[asdict(rule) for rule in self.cog.tactics.rules(
                        room['guild_id'], user_id, state['job'])],
                    passive_id=passive.id if passive else None,
                    basic_target=self.cog.tactics.basic_target(room['guild_id'], user_id, state['job']),
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
            if room['boss'] == WITCH_BOSS:
                battle = witch_battle_from_participants(participants, room['witch_ids'], seed=seed)
            elif room['boss'] == NOAH_JOB:
                battle = noah_total_battle_from_participants(participants, seed=seed, max_rounds=30)
            else:
                battle = training_dummy_battle_from_participants(
                    participants, seed=seed, max_rounds=self.settings.max_rounds)
            room.update(
                status='running', members=[item['id'] for item in participants],
                participants=participants, seed=seed, battle=dump_total_battle(battle),
                round_deadline=time.time() + (WITCH_ROUND_SECONDS if room['boss'] == WITCH_BOSS else self.settings.action_timeout_seconds),
            )
            if room['boss'] == WITCH_BOSS:
                self.repo.start_witch(room)
            else:
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

    def player_action_text(self, battle, user_id):
        actor = next(fighter for fighter in battle.fighters
                     if fighter.team == 0 and fighter.user_id == user_id)
        lines = [f'第 {battle.planning_round} 回合｜{actor.name}｜HP {actor.hp:,}/{actor.stats["HP"]:,}',
                 '', '技能冷卻：']
        for action in (item for item in battle.available_actions(user_id) if item['action'] == ACTION_SKILL):
            cooldown = action['cooldown_remaining']
            state = '可使用' if cooldown == 0 else f'CD {cooldown} 回合'
            lines.append(f'• 槽 {action["skill_slot"]}【{action["name"]}】：{state}')
        choice = battle.choices.get(user_id)
        if choice is not None:
            if choice.action == ACTION_ATTACK:
                selected = '普通攻擊'
            elif choice.action == ACTION_DEFEND:
                selected = '防禦'
            else:
                if choice.action == ACTION_CANVAS:
                    selected = f'踏入畫布・{PAINT_NAMES[{"red": 1, "yellow": 2, "blue": 4}[choice.target]]}'
                else:
                    rule = next(rule for rule in actor.rules if rule.slot == choice.skill_slot)
                    selected = f'【{rule_skill(actor.job, rule).name}】'
            lines.extend(('', f'目前已登記：{selected}（可在結算前修改）'))
        paint = battle.paint_mask(actor)
        if battle.noah_phase() == 2:
            effect = f'（{PAINT_EFFECTS[paint]}）' if paint else ''
            lines.extend(('', f'目前顏料：{PAINT_NAMES[paint]}{effect}'))
            gift = battle.paint_gifts.get(user_id)
            receiver = battle.fighter_for_key(gift) if gift else None
            if receiver is not None:
                lines.append(f'已登記給予：{receiver.name}')
        if isinstance(battle, WitchRaidBattle):
            lines.append('選擇後請按「確認本回合」；修改行動會取消確認。')
            if battle.washed(actor, battle.planning_round):
                lines.append('洗腦：強制普攻隊友。' if battle.forced(actor, battle.planning_round) else '洗腦：攻擊打隊友，治療給魔女；可防禦或安全支援。')
        lines.extend(('', '請選擇本回合行動。'))
        return '\n'.join(lines)

    async def confirm_action(self, room_id, user_id):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            await self.check_witch_deadline(room, battle)
            if str(user_id) in room.get('action_drafts', {}):
                raise TotalRaidError('請先選擇目標，完成本回合行動。')
            battle.confirm(user_id)
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            if battle.ready_to_resolve():
                await self._resolve(room, battle)
            else:
                await self._edit_public(room, battle)

    async def takeover_action(self, room_id, user_id):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            await self.check_witch_deadline(room, battle)
            battle.takeover(user_id)
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            await self._edit_public(room, battle)

    async def check_witch_deadline(self, room, battle):
        if isinstance(battle, WitchRaidBattle) and time.time() >= room['round_deadline']:
            await self._resolve(room, battle, timeout=True)
            raise TotalRaidError('本回合已逾時並結算，請重新開啟行動面板。')

    async def submit_action(self, room_id, user_id, action, target, skill_slot, expected_round=None):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            await self.check_witch_deadline(room, battle)
            if expected_round is not None and battle.planning_round != expected_round:
                raise TotalRaidError('此面板的回合已結束，請重新開啟行動面板。')
            battle.submit(user_id, action, target, skill_slot)
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            if battle.ready_to_resolve():
                await self._resolve(room, battle)
            else:
                await self._edit_public(room, battle)

    async def submit_paint_gift(self, room_id, user_id, target):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            battle.submit_paint_gift(user_id, target)
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            await self._edit_public(room, battle)

    async def public_choice(self, room_id, user_id, expected_round, value, is_target=False):
        async with self.lock(room_id):
            room, battle = self.running_battle(room_id, user_id)
            await self.check_witch_deadline(room, battle)
            if battle.planning_round != expected_round:
                raise TotalRaidError('回合已更新，請使用最新戰鬥面板。')
            drafts = room.setdefault('action_drafts', {})
            if is_target:
                draft = drafts.get(str(user_id))
                if not draft or draft['round'] != expected_round:
                    raise TotalRaidError('請先在第一個選單選擇行動，再選擇目標。')
                battle.submit(user_id, draft['action'], value, draft['slot'])
                drafts.pop(str(user_id))
                message = '行動已登記，請按「確認本回合」。'
            else:
                action, _, detail = value.partition(':')
                slot = int(detail) if detail else None
                available = next((a for a in battle.available_actions(user_id)
                                  if a['action'] == action and a.get('skill_slot') == slot), None)
                if not available or available.get('cooldown_remaining', 0):
                    raise TotalRaidError('此技能未裝備、冷卻中或目前不能使用。')
                targets = battle.valid_targets(user_id, action, slot)
                if targets:
                    drafts[str(user_id)] = dict(action=action, slot=slot, round=expected_round)
                    battle.confirmed.discard(user_id)
                    battle.choices.pop(user_id, None)
                    battle.auto_players.discard(user_id)
                    message = '已選擇行動，請在第二個選單選擇目標：' + '、'.join(
                        battle.fighter_for_key(k).name for k in targets)
                else:
                    battle.submit(user_id, action, None, slot)
                    drafts.pop(str(user_id), None)
                    message = '行動已登記，不需指定目標；請按「確認本回合」。'
            room['battle'] = dump_total_battle(battle)
            self.repo.save(room)
            await self._edit_public(room, battle)
            return message

    async def _resolve(self, room, battle, timeout=False):
        battle.resolve(use_defaults=timeout)
        room['log_page'] = 0
        room['status_page'] = 0
        room['action_drafts'] = {}
        room['battle'] = dump_total_battle(battle)
        if battle.result:
            room['status'] = 'completed'
            room['finished_at'] = time.time()
            room['round_deadline'] = None
            view = self.views.pop((room['id'], 'running'), None)
            if view:
                view.stop()
        else:
            room['round_deadline'] = time.time() + (WITCH_ROUND_SECONDS if room['boss'] == WITCH_BOSS else self.settings.action_timeout_seconds)
        if room['boss'] == WITCH_BOSS and battle.result:
            self.repo.finish_witch(room, battle)
        else:
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
        if room.get('public_pending'):
            room['public_pending'] = False
            self.repo.save(room)

    def lobby_embed(self, room):
        roster = '\n'.join(f'<@{user_id}>' for user_id in room['members'])
        embed = discord.Embed(
            title=f'總力戰測試｜{room["boss"]} #{room["number"]}', color=0x8B5CF6,
            description='加入隊伍後，由房主決定何時開始。測試版不消耗噴漆罐套組，也不發放獎勵。',
        )
        if room['boss'] == WITCH_BOSS:
            embed.title = f'魔女試煉｜{room["witch_day"]} #{room["number"]}'
            embed.description = ('、'.join(PROFILE[k][1] for k in room['witch_ids']) +
                '\n由房主開始，所有玩家免費入場。勝利每人取得 1 個魔女繡線。\n每回合 120 秒，全員確認可提早結算；連續三回合逾時轉自動普攻，可隨時接管。')
        embed.add_field(name='房主', value=f'<@{room["host_id"]}>')
        embed.add_field(name='隊伍', value=f'{len(room["members"])}/{self.settings.max_participants}', inline=True)
        embed.add_field(name='參戰成員', value=roster or '尚無成員', inline=False)
        embed.set_footer(text='房主建立房間時會自動加入；目前最多六人。')
        if room['boss'] == WITCH_BOSS:
            embed.set_footer(text='待機房建立 30 分鐘後自動關閉；戰鬥結束後戰報送至魔女試煉頻道並刪除房間。')
        return embed

    def daily_embed(self):
        day, ids = self.repo.daily_witches()
        embed = discord.Embed(title=f'魔女試煉｜{day}', color=0x8B5CF6,
            description='今日出現：\n' + '\n'.join(f'• {PROFILE[k][1]}' for k in ids) +
            '\n\n點擊開房，最多六人。每日台灣時間 00:00 更新；舊公告按鈕也會開啟當日組合。'
            '\n所有玩家免費入場；勝利每人獲得 1 個不可交易的魔女繡線。')
        for key in ids:
            embed.add_field(name=PROFILE[key][1], value='\n'.join(
                f'{label}：{ability}' for label, ability in zip(('一般', '一次魔女化', '二次魔女化'), SPELL_DESCRIPTIONS[key]))
                + '\n特性：' + WITCH_TRAITS[key], inline=False)
        embed.set_footer(text='每位魔女同伴首次倒下使魔女化加深一階；每階傷害與治療 +10%，最多二階。')
        return embed

    async def witch_announcement_channels(self):
        if self.witch_channel_ids:
            return [channel for cid in sorted(self.witch_channel_ids)
                    if isinstance(channel := self.bot.get_channel(cid), discord.TextChannel)]
        # Only create in guilds with an explicitly configured raid category.
        categories = {}
        for cid in sorted(self.category_ids):
            category = self.bot.get_channel(cid)
            if isinstance(category, discord.CategoryChannel):
                categories.setdefault(category.guild.id, category)
        channels = []
        for gid, category in categories.items():
            if time.time() < self.witch_channel_retry_at.get(gid, 0):
                continue
            async with self.lock(('witch_announcement', gid)):
                try:
                    row = self.repo.db.execute('SELECT channel_id FROM rpg_witch_channels WHERE guild_id=?', (gid,)).fetchone()
                    channel = self.bot.get_channel(row[0]) if row else None
                    if row and channel is None:
                        # Cache misses are not proof of deletion; confirm through Discord.
                        try:
                            channel = await self.bot.fetch_channel(row[0])
                        except discord.NotFound:
                            pass
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != gid:
                        channel = next((c for c in category.text_channels if c.name == '魔女試煉'), None)
                    if channel is None:
                        guild = category.guild
                        channel = await category.create_text_channel(
                            name='魔女試煉', topic='每日 00:00 公布三位魔女；點擊公告開啟最多六人的魔女試煉。',
                            overwrites={
                                guild.default_role: discord.PermissionOverwrite(
                                    view_channel=True, send_messages=False, read_message_history=True),
                                guild.me: discord.PermissionOverwrite(
                                    view_channel=True, send_messages=True, read_message_history=True,
                                    embed_links=True, manage_channels=True),
                            }, reason='建立每日魔女試煉公告頻道')
                    with self.repo.db:
                        self.repo.db.execute('''INSERT INTO rpg_witch_channels VALUES (?,?)
                            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id''',
                            (gid, channel.id))
                    channels.append(channel)
                except discord.HTTPException:
                    self.witch_channel_retry_at[gid] = time.time() + 300
                    logger.exception('Cannot create or find witch announcement channel: %s', gid)
        return channels

    async def announce_witches(self):
        if not self.settings.enabled:
            return
        day = witch_day()
        for channel in await self.witch_announcement_channels():
            cid = channel.id
            saved = self.repo.db.execute('SELECT day,message_id FROM rpg_witch_announcements WHERE channel_id=?', (cid,)).fetchone()
            if saved and saved[0] == day:
                if (cid, day) in self.refreshed_announcements:
                    continue
                try:
                    await channel.get_partial_message(saved[1]).edit(embed=self.daily_embed(), view=WitchDailyView(self),
                                                                    allowed_mentions=discord.AllowedMentions.none())
                    self.refreshed_announcements.add((cid, day))
                    continue
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    logger.exception('Witch daily announcement refresh failed: %s', cid)
                    continue
            try:
                message = await channel.send(embed=self.daily_embed(), view=WitchDailyView(self),
                                             allowed_mentions=discord.AllowedMentions.none())
                with self.repo.db:
                    self.repo.db.execute('''INSERT INTO rpg_witch_announcements VALUES (?,?,?)
                        ON CONFLICT(channel_id) DO UPDATE SET day=excluded.day,message_id=excluded.message_id''',
                        (cid, day, message.id))
                self.refreshed_announcements.add((cid, day))
            except discord.HTTPException:
                logger.exception('Witch daily announcement failed: %s', cid)

    async def cleanup_witch_rooms(self, now):
        rows = self.repo.db.execute('SELECT data FROM rpg_total_raids').fetchall()
        for raw, in rows:
            room = json.loads(raw)
            if room['boss'] != WITCH_BOSS or room.get('channel_deleted') or room['status'] == 'running':
                continue
            if room.get('public_pending') and room.get('battle'):
                try:
                    await self._edit_public(room, load_total_battle(room['battle']))
                except discord.HTTPException:
                    logger.exception('Witch result delivery retry failed: %s', room['id'])
            expiry = room['created_at'] + 1800 if room['status'] == 'lobby' else 0
            if now < expiry:
                continue
            async with self.lock(room['id']):
                room = self.repo.get(room['id'])
                if room['status'] == 'running':
                    continue
                room['status'] = 'cancelled' if room['status'] == 'lobby' else room['status']
                self.repo.save(room)
                if not room.get('archive_message_id'):
                    destinations = await self.witch_announcement_channels()
                    destination = next((c for c in destinations if c.guild.id == room['guild_id']
                                        and c.id != room['channel_id']), None)
                    if destination is None:
                        continue
                    battle = load_total_battle(room['battle']) if room.get('battle') else None
                    result = battle.result if battle else '待機房已關閉，未開戰'
                    names = '、'.join(PROFILE[k][1] for k in room.get('witch_ids', []))
                    header = f'魔女試煉 #{room["number"]}｜{result}\n{names}\n' + room.get('reward_text', '')
                    records = []
                    if battle:
                        records = (battle.mechanics.get('witch_initial_log', []) +
                                   [line for turn in battle.mechanics.get('witch_round_logs', []) for line in turn]) or battle.log
                        header += '\n參戰：' + '、'.join(f'{p.name}（{p.user_id}）' for p in battle.fighters if p.team == 0)
                    payload = (header + '\n\n' + '\n'.join(records)).encode('utf-8-sig')
                    try:
                        message = await destination.send(
                            embed=discord.Embed(title=f'魔女試煉 #{room["number"]}｜戰鬥紀錄', description=header, color=0x8B5CF6),
                            file=discord.File(io.BytesIO(payload), filename=f'witch-trial-{room["number"]}.txt'),
                            allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        logger.exception('Witch archive delivery failed: %s', room['id'])
                        continue
                    room['archive_message_id'] = message.id
                    room['archive_channel_id'] = destination.id
                    self.repo.save(room)
                channel = self.bot.get_channel(room['channel_id'])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(room['channel_id'])
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        continue
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.delete(reason='魔女試煉暫時房間到期')
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        logger.exception('Witch room cleanup failed: %s', room['id'])
                        continue
                room['status'] = 'cancelled' if room['status'] == 'lobby' else room['status']
                room['channel_deleted'] = True
                self.repo.save(room)
                for status in ('lobby', 'running'):
                    view = self.views.pop((room['id'], status), None)
                    if view:
                        view.stop()

    def battle_embed(self, room, battle):
        status = battle.result or f'第 {battle.planning_round} 回合・選擇行動'
        embed = discord.Embed(
            title=f'{"魔女試煉" if room["boss"] == WITCH_BOSS else room["boss"]} #{room["number"]}｜{status}', color=0xDC2626,
        )
        enemy_lines = []
        for fighter in (item for item in battle.fighters if item.team == 1 and
                        (not isinstance(battle, WitchRaidBattle) or item.job in battle.ids or item.hp > 0)):
            buffs, debuffs = effect_status(fighter, battle)
            enemy_lines.append(
                f'**{fighter.name}**\n{hp_bar(fighter.hp, fighter.stats["HP"])}  '
                f'{fighter.hp:,}/{fighter.stats["HP"]:,}\n'
                f'Buff：{buffs}\nDebuff：{debuffs}')
        enemies = '\n\n'.join(enemy_lines)
        for index, chunk in enumerate(field_chunks(enemy_lines), 1):
            embed.add_field(name='Boss HP' if index == 1 else f'Boss HP（{index}）', value=chunk, inline=False)
        if not battle.result:
            intent = battle.intent()
            action = f'**{intent.name}**\n{intent.description}'
        else:
            action = '戰鬥已結束。'
        for index, chunk in enumerate(field_chunks(action.splitlines()), 1):
            embed.add_field(name='Boss 行動' if index == 1 else f'Boss 行動（{index}）', value=chunk, inline=False)
        waiting = battle.waiting_player_ids() if not battle.result else set()
        roster = []
        for fighter in (item for item in battle.fighters if item.team == 0):
            marker = '💀' if fighter.hp <= 0 else ('⌛' if fighter.user_id in waiting else '✅')
            if isinstance(battle, WitchRaidBattle) and fighter.hp > 0 and fighter.user_id in battle.auto_players:
                marker = '🤖'
            buffs, debuffs = effect_status(fighter, battle)
            roster.append(f'{marker} <@{fighter.user_id}>：{fighter.hp:,}/{fighter.stats["HP"]:,}\n'
                          f'　Buff：{buffs}\n　Debuff：{debuffs}')
            if isinstance(battle, WitchRaidBattle) and fighter.hp > 0:
                skills = [f'{a["skill_slot"]}：{a["name"]}（CD {a.get("cooldown_remaining", 0)}）'
                          for a in battle.available_actions(fighter.user_id) if a['action'] == ACTION_SKILL]
                choice = battle.choices.get(fighter.user_id)
                selected = '尚未選擇'
                if choice:
                    selected = (f'技能 {choice.skill_slot}' if choice.action == ACTION_SKILL else
                                '防禦' if choice.action == ACTION_DEFEND else '普攻')
                    target = battle.fighter_for_key(choice.target)
                    if target:
                        selected += f' → {target.name}'
                if str(fighter.user_id) in room.get('action_drafts', {}):
                    selected = '已選行動，等待指定目標'
                roster[-1] += '\n　' + '／'.join(skills) + f'\n　行動：{selected}'
        for index, chunk in enumerate(field_chunks(roster), 1):
            name = '隊伍狀態' if index == 1 else f'隊伍狀態（{index}）'
            embed.add_field(name=name, value=chunk, inline=False)
        if isinstance(battle, WitchRaidBattle):
            for index, chunk in enumerate(field_chunks(witch_transformation_notes(battle), 900), 1):
                embed.add_field(name=f'魔女化效果（{index}）', value=chunk, inline=False)
            effect_notes = [f'**{name}**：{description}' if separator else f'**{name}**'
                            for name, separator, description in
                            (note.partition('：') for note in active_effect_notes(battle))]
            for index, chunk in enumerate(field_chunks(effect_notes, 900), 1):
                embed.add_field(name=f'增益／減益效果說明（{index}）', value=chunk, inline=False)
            # Keep every state and explanation accessible within Discord's 6,000 character limit.
            state_pages, current, size = [], [], 0
            for field in embed.fields:
                length = len(field.name) + len(field.value)
                if current and size + length > 4000:
                    state_pages.append(current)
                    current, size = [], 0
                current.append(field)
                size += length
            if current:
                state_pages.append(current)
            room['status_page_count'] = len(state_pages)
            state_page = room.get('status_page', 0) % len(state_pages)
            embed.clear_fields()
            for field in state_pages[state_page]:
                embed.add_field(name=field.name, value=field.value, inline=False)
            if len(state_pages) > 1:
                embed.description = f'狀態與效果說明 {state_page + 1}/{len(state_pages)} 頁；使用下方「狀態／說明」按鈕翻頁。'
        # Bound the public embed; full battle state remains persisted.
        log_lines = last_round_log(battle)
        if isinstance(battle, WitchRaidBattle):
            pages = field_chunks(log_lines, 900)
            page = room.get('log_page', 0) % max(1, len(pages))
            log_lines = [pages[page]] if pages else []
        for index, chunk in enumerate(field_chunks(log_lines), 1):
            label = '上一回合摘要' if isinstance(battle, WitchRaidBattle) else '上一回合完整摘要'
            if isinstance(battle, WitchRaidBattle):
                label += f'（{page + 1}/{len(pages)} 頁）'
            name = label if index == 1 else f'{label}（{index}）'
            embed.add_field(name=name, value=chunk, inline=False)
        if not battle.result:
            embed.add_field(name='行動期限', value=f'<t:{int(room["round_deadline"])}:R>', inline=False)
            embed.set_footer(text='點擊下方按鈕開啟私人面板；✅ 僅表示已完成選擇，不公開實際行動。')
        else:
            embed.set_footer(text='測試版不發放獎勵；頻道目前保留供檢查戰報。')
        if room['boss'] == WITCH_BOSS:
            embed.set_footer(text=room.get('reward_text', '選擇後需確認；逾時普攻，連續三次轉自動。🤖 可接管。'))
        return embed

    @tasks.loop(seconds=5)
    async def tick(self):
        if not self.bot.is_ready():
            return
        now = time.time()
        await self.announce_witches()
        await self.cleanup_witch_rooms(now)
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
