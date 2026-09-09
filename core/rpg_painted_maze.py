"""Persistent room lifecycle for the Painted Maze roguelite mode."""
import json
import random
import secrets
import time
import uuid

from core.rpg_character import CharacterError
from core.rpg_maze_traits import descriptions as trait_descriptions


MODE_NAME = '繪境迷廊'
ENTRY_ENABLED = False
ENTRY_CLOSED_NOTICE = '繪境迷廊目前暫停開放，正在調整中；入場畫作不會消耗。'
MIN_LEVEL = 50
MAX_PARTICIPANTS = 8
ROOM_LIFETIME_SECONDS = 24 * 60 * 60
CONTRACT_VOTE_SECONDS = 60
ACTIVE_STATUSES = ('lobby', 'running', 'contract')
ENTRY_ROUTES = {
    'noah:unfinished': 'noah',
    'painting:balloon': 'shadow',
}
PAINTING_STAGES = (
    (
        {'id': 'charcoal_beast', 'name': '《炭筆獄獸》', 'base_kind': '巨獸', 'tier': 55},
        {'id': 'wire_guard', 'name': '《鐵線守衛》', 'base_kind': '鐵殼魔像', 'tier': 55},
        {'id': 'moon_fox_study', 'name': '《月下狐之習作》', 'base_kind': '月影妖狐', 'tier': 55},
        {'id': 'ink_spider', 'name': '《墨毒蜘蛛》', 'base_kind': '毒蛛', 'tier': 55},
        {'id': 'thorn_sketch', 'name': '《荊棘庭園素描》', 'base_kind': '荊棘妖樹', 'tier': 55},
    ),
    (
        {'id': 'twin_zodiac', 'name': '《赤雷與蒼炎的雙生肖像》', 'base_kind': '赤雷與蒼炎', 'tier': 60},
        {'id': 'sunken_whale', 'name': '《沉沒城邦與巨鯨》', 'base_kind': '吞城鯨', 'tier': 60},
        {'id': 'stitched_color', 'name': '《腐彩縫合像》', 'base_kind': '瘟疫縫合獸', 'tier': 60},
        {'id': 'mist_still_life', 'name': '《霧菌靜物》', 'base_kind': '迷霧菌后', 'tier': 60},
        {'id': 'faceless_group', 'name': '《無面傀儡群像》', 'base_kind': '王城傀儡師', 'tier': 60},
    ),
    (
        {'id': 'burning_armor', 'name': '《燃燒鎧甲的末日壁畫》', 'base_kind': '熔爐鎧獸', 'tier': 65},
        {'id': 'star_eclipse', 'name': '《星蝕巨神天頂畫》', 'base_kind': '星蝕巨神', 'tier': 65},
        {'id': 'reverse_tide', 'name': '《逆潮聖骸祭壇畫》', 'base_kind': '逆潮聖骸', 'tier': 65},
        {'id': 'light_eating_dragon', 'name': '《吞噬光芒的鐘龍》', 'base_kind': '深淵鐘龍', 'tier': 65},
        {'id': 'unrecorded_colossus', 'name': '《未被記錄的黑色巨像》', 'base_kind': '鐵殼魔像', 'tier': 65},
    ),
)
COLOR_CONTRACTS = {
    'crimson': {
        'name': '緋紅契約',
        'party': '全隊直接傷害 +8%',
        'backlash': '最終敵人會在玩家累積直接命中後反擊，疊層越高越頻繁',
    },
    'azure': {
        'name': '蒼藍契約',
        'party': '全隊受到直接傷害 -6%',
        'backlash': '最終敵人每次階段轉換取得最大 HP 護盾',
    },
    'gold': {
        'name': '金黃契約',
        'party': '全隊速度 +8；持有疾筆且金黃契約合計兩層時，主動技能基礎冷卻 -1',
        'backlash': '最終敵人速度提高；兩層以上時蓄力提前完成',
    },
    'verdant': {
        'name': '翠綠契約',
        'party': '全隊治療量 +10%，過關恢復 +3% 最大 HP',
        'backlash': '最終敵人每三回合恢復 0.5% 最大 HP／層',
    },
    'violet': {
        'name': '紫蝕契約',
        'party': '對有負面狀態的敵人傷害 +8%；施加的中毒、毒箭與虛弱延長 1 回合／層，不延長暈眩或破甲',
        'backlash': '最終敵人命中時附加腐敗',
    },
    'black': {
        'name': '漆黑契約',
        'party': '全隊傷害 +12%、暴擊率 +2 百分點，但受到傷害 +4%',
        'backlash': '最終敵人傷害 +8%；三層時每五回合追加行動',
    },
}

# Keep the original IDs valid for rooms saved before the contract expansion.
CONTRACT_COLORS = tuple(COLOR_CONTRACTS)
for _color, _contract in COLOR_CONTRACTS.items():
    _contract['color'] = _color
CONTRACT_VARIANTS = {
    'crimson': [('edge', '銳筆'), ('precision', '點睛')],
    'azure': [('armor', '厚塗'), ('vitality', '留白')],
    'gold': [('aim', '聚光'), ('evasion', '掠影')],
    'verdant': [('renewal', '回春'), ('shelter', '庇蔭')],
    'violet': [('focus', '蝕刻'), ('insight', '洞察')],
    'black': [('ruin', '毀形'), ('gamble', '孤注')],
}
_trait_descriptions = trait_descriptions()
for _color, _variants in CONTRACT_VARIANTS.items():
    for _suffix, _name in _variants:
        _key = f'{_color}:{_suffix}'
        COLOR_CONTRACTS[_key] = {
            'color': _color, 'name': f'{COLOR_CONTRACTS[_color]["name"]}・{_name}',
            'party': _trait_descriptions[_key], 'backlash': COLOR_CONTRACTS[_color]['backlash'],
        }


# Give the original choices the same naming style without changing saved IDs.
for _color, _name in {
    'crimson': '濃彩', 'azure': '薄幕', 'gold': '疾筆',
    'verdant': '滋養', 'violet': '侵染', 'black': '狂墨',
}.items():
    COLOR_CONTRACTS[_color]['name'] += f'・{_name}'


def draw_painting_route(seed):
    rng = random.Random(seed)
    return [dict(rng.choice(stage)) for stage in PAINTING_STAGES]


def paintings_per_stage(room):
    """New rooms have one painting per act; retain already saved legacy routes."""
    return max(1, len(room['paintings']) // 3)


class PaintedMazeError(CharacterError):
    pass


class PaintedMazeStore:
    """Own room state and its inventory transaction boundaries.

    Discord objects are deliberately stored as IDs.  The service layer may recreate
    views after a restart without having to rebuild or reroll a room.
    """

    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_painted_maze_rooms (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, host_id INTEGER NOT NULL,
                thread_id INTEGER UNIQUE, status TEXT NOT NULL, expires_at REAL NOT NULL,
                data TEXT NOT NULL)''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_painted_maze_active
                ON rpg_painted_maze_rooms(guild_id,status,expires_at)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_painted_maze_numbers (
                guild_id INTEGER PRIMARY KEY, next_number INTEGER NOT NULL)''')

    @staticmethod
    def _decode(row):
        return json.loads(row[0]) if row else None

    def get(self, room_id):
        return self._decode(self.db.execute(
            'SELECT data FROM rpg_painted_maze_rooms WHERE id=?', (room_id,)).fetchone())

    def by_thread(self, thread_id):
        return self._decode(self.db.execute(
            'SELECT data FROM rpg_painted_maze_rooms WHERE thread_id=?', (thread_id,)).fetchone())

    def active(self, guild_id=None):
        query = ("SELECT data FROM rpg_painted_maze_rooms "
                 "WHERE status IN ('lobby','running','contract')")
        args = ()
        if guild_id is not None:
            query += ' AND guild_id=?'
            args = (guild_id,)
        return [json.loads(row[0]) for row in self.db.execute(query, args).fetchall()]

    def active_for_user(self, guild_id, user_id, *, exclude=None):
        return next((room for room in self.active(guild_id)
                     if room['id'] != exclude and user_id in room['members']), None)

    def _reserve_number(self, guild_id):
        row = self.db.execute(
            'SELECT next_number FROM rpg_painted_maze_numbers WHERE guild_id=?',
            (guild_id,),
        ).fetchone()
        number = row[0] if row else 1
        self.db.execute('''INSERT INTO rpg_painted_maze_numbers VALUES (?,?)
            ON CONFLICT(guild_id) DO UPDATE SET next_number=excluded.next_number''',
            (guild_id, number + 1))
        return number

    def _save(self, room):
        self.db.execute('''UPDATE rpg_painted_maze_rooms
            SET thread_id=?,status=?,expires_at=?,data=? WHERE id=?''',
            (room.get('thread_id'), room['status'], room['expires_at'],
             json.dumps(room, ensure_ascii=False), room['id']))

    def create(self, guild_id, host_id, entry_item, host_level, *, channel_id=None,
               now=None, seed=None, require_entry=True):
        if entry_item not in ENTRY_ROUTES:
            raise PaintedMazeError('這件物品不能開啟繪境迷廊。')
        if host_level < MIN_LEVEL:
            raise PaintedMazeError(f'繪境迷廊需要 Lv.{MIN_LEVEL} 才能進入。')
        now = time.time() if now is None else now
        seed = secrets.randbits(63) if seed is None else seed
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.active_for_user(guild_id, host_id):
                raise PaintedMazeError('你已在這個伺服器的另一個繪境迷廊房間中。')
            if require_entry:
                owned = self.db.execute('''SELECT quantity FROM rpg_inventory
                    WHERE guild_id=? AND user_id=? AND item_id=?''',
                    (guild_id, host_id, entry_item)).fetchone()
                if not owned or owned[0] < 1:
                    raise PaintedMazeError('背包中沒有可用的入場畫作。')
            number = self._reserve_number(guild_id)
            room = {
                'id': uuid.uuid4().hex,
                'guild_id': guild_id,
                'host_id': host_id,
                'number': number,
                'entry_item': entry_item,
                'requires_entry': require_entry,
                'reward_policy': 'escrow_v2',
                'rest_ready': [],
                'route': ENTRY_ROUTES[entry_item],
                'status': 'lobby',
                'members': [host_id],
                'participants': [],
                'channel_id': channel_id,
                'thread_id': None,
                'index_message_id': None,
                'message_id': None,
                'created_at': now,
                'started_at': None,
                'expires_at': now + ROOM_LIFETIME_SECONDS,
                'ended_at': None,
                'ended_by': None,
                'end_reason': None,
                'entry_consumed': False,
                'seed': seed,
                'paintings': draw_painting_route(seed),
                'stage': 0,
                'boss_index': 0,
                'contracts': [],
                'contract_vote': None,
                'sealed_rewards': [],
                'reward_due': [],
                'party_state': {},
                'battle_history': [],
                'last_battle': None,
            }
            self.db.execute('''INSERT INTO rpg_painted_maze_rooms
                (id,guild_id,host_id,thread_id,status,expires_at,data)
                VALUES (?,?,?,?,?,?,?)''',
                (room['id'], guild_id, host_id, None, room['status'], room['expires_at'],
                 json.dumps(room, ensure_ascii=False)))
            return room

    def attach_discord(self, room_id, *, channel_id, thread_id, index_message_id, message_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] != 'lobby':
                raise PaintedMazeError('這個房間已不存在或已經關閉。')
            room.update(channel_id=channel_id, thread_id=thread_id,
                        index_message_id=index_message_id, message_id=message_id)
            self._save(room)
            return room

    def mark_report_sent(self, room_id, message_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            room['report_message_id'] = message_id
            self._save(room)
            return room

    def change_member(self, room_id, user_id, level, *, leave=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] != 'lobby':
                raise PaintedMazeError('房間已經開始或關閉。')
            if now >= room['expires_at']:
                raise PaintedMazeError('這個房間已經逾期。')
            if leave:
                if user_id == room['host_id']:
                    raise PaintedMazeError('房主不能退出自己的隊伍。')
                if user_id not in room['members']:
                    raise PaintedMazeError('你尚未加入這個隊伍。')
                room['members'].remove(user_id)
            else:
                if level < MIN_LEVEL:
                    raise PaintedMazeError(f'繪境迷廊需要 Lv.{MIN_LEVEL} 才能進入。')
                if user_id in room['members']:
                    raise PaintedMazeError('你已經在隊伍中。')
                if len(room['members']) >= MAX_PARTICIPANTS:
                    raise PaintedMazeError('繪境迷廊隊伍已滿。')
                if self.active_for_user(room['guild_id'], user_id, exclude=room_id):
                    raise PaintedMazeError('你已在另一個繪境迷廊房間中。')
                room['members'].append(user_id)
            self._save(room)
            return room

    def begin(self, room_id, actor_id, participants, *, now=None):
        """Consume the host's painting and lock snapshots in the same transaction."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] != 'lobby':
                raise PaintedMazeError('這個房間已經開始或關閉。')
            if actor_id != room['host_id']:
                raise PaintedMazeError('只有房主可以開始繪境迷廊。')
            if now >= room['expires_at']:
                raise PaintedMazeError('這個房間已經逾期。')
            participant_ids = [participant['id'] for participant in participants]
            if participant_ids != room['members']:
                raise PaintedMazeError('隊伍快照不完整，尚未消耗入場畫作。')
            if not 1 <= len(participants) <= MAX_PARTICIPANTS:
                raise PaintedMazeError('隊伍人數必須為 1～8 人。')
            if any(participant.get('state', {}).get('level', 0) < MIN_LEVEL
                   for participant in participants):
                raise PaintedMazeError(f'所有隊員都必須達到 Lv.{MIN_LEVEL}。')
            requires_entry = room.get('requires_entry', True)
            if requires_entry:
                paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                    WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=1''',
                    (room['guild_id'], room['host_id'], room['entry_item']))
                if not paid.rowcount:
                    raise PaintedMazeError('房主已沒有這張入場畫作。')
                self.db.execute('''DELETE FROM rpg_inventory
                    WHERE guild_id=? AND user_id=? AND item_id=? AND quantity=0''',
                    (room['guild_id'], room['host_id'], room['entry_item']))
            room.update(
                status='running', participants=participants, entry_consumed=requires_entry,
                started_at=now, expires_at=now + ROOM_LIFETIME_SECONDS,
                party_state={str(participant['id']): {
                    'hp': participant['state']['combat']['HP'],
                    'max_hp': participant['state']['combat']['HP'],
                    'fallen': False,
                } for participant in participants},
            )
            self._save(room)
            return room

    def settle_painting(self, room_id, actor_id, painting_index, result,
                        battle, party_state, *, sealed_rewards=None, now=None):
        """Persist one fully simulated battle exactly once."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room:
                raise PaintedMazeError('找不到繪境迷廊房間。')
            prior = next((item for item in room.get('battle_history', [])
                          if item['painting_index'] == painting_index), None)
            if prior:
                if prior['result'] != result:
                    raise PaintedMazeError('這幅畫作已使用不同結果完成結算。')
                return room
            if room['status'] != 'running':
                raise PaintedMazeError('目前不能結算畫作戰鬥。')
            if actor_id not in room['members']:
                raise PaintedMazeError('只有隊伍成員可以推進繪境迷廊。')
            if painting_index != room['boss_index'] or painting_index >= len(room['paintings']):
                raise PaintedMazeError('畫作進度與房間狀態不一致。')
            if result not in ('勝利', '戰敗', '平手', '平手（達回合上限）'):
                raise PaintedMazeError('無效的畫作戰鬥結果。')
            summary = {
                'painting_index': painting_index,
                'painting_id': room['paintings'][painting_index]['id'],
                'result': result,
                'rounds': battle.get('round', 0),
                'completed_at': now,
            }
            room['battle_history'].append(summary)
            room.setdefault('battle_reports', []).append({
                'title': room['paintings'][painting_index]['name'], 'battle': battle})
            room['last_battle'] = battle
            room.pop('battle', None)
            room.pop('battle_deadline', None)
            room['party_state'] = party_state
            if result != '勝利':
                self._finish(room, 'failed', actor_id, f'畫作戰鬥{result}', now)
                self._save(room)
                return room
            room['boss_index'] += 1
            room['rest_ready'] = []
            if sealed_rewards:
                room['sealed_rewards'].extend(sealed_rewards)
            if room['boss_index'] % paintings_per_stage(room) == 0:
                contract_round = room['boss_index'] // paintings_per_stage(room)
                room.setdefault('reward_due', []).append({
                    'kind': 'stage', 'checkpoint': contract_round})
                room['stage'] = contract_round
                room['status'] = 'contract'
                room['contract_vote'] = {
                    'round': contract_round,
                    'candidates': self._contract_candidates(room, contract_round),
                    'votes': {},
                    'opened_at': now,
                    'deadline': now + CONTRACT_VOTE_SECONDS,
                }
            self._save(room)
            return room

    def settle_final(self, room_id, actor_id, result, battle, party_state, *, now=None):
        """Persist the final battle exactly once; loot is sealed separately and idempotently."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room:
                raise PaintedMazeError('找不到繪境迷廊房間。')
            prior = room.get('final_battle')
            if prior:
                if prior['result'] != result:
                    raise PaintedMazeError('尾王已使用不同結果完成結算。')
                return room
            if room['status'] != 'running' or room.get('boss_index') != len(room['paintings']):
                raise PaintedMazeError('目前不能結算最終戰鬥。')
            if actor_id not in room['members']:
                raise PaintedMazeError('只有隊伍成員可以結算最終戰鬥。')
            if result not in ('勝利', '戰敗', '平手', '平手（達回合上限）'):
                raise PaintedMazeError('無效的最終戰鬥結果。')
            room['final_battle'] = {
                'route': room['route'], 'result': result,
                'rounds': battle.get('round', 0), 'completed_at': now,
            }
            room['final_entered'] = True
            room.setdefault('battle_reports', []).append({'title': '最終畫室', 'battle': battle})
            room['last_battle'] = battle
            room.pop('battle', None)
            room.pop('battle_deadline', None)
            room['party_state'] = party_state
            if result == '勝利':
                room.setdefault('reward_due', []).append({'kind': 'final', 'checkpoint': 4})
                self._finish(room, 'completed', actor_id, '完成繪境迷廊', now)
            else:
                self._finish(room, 'failed', actor_id, f'最終戰鬥{result}', now)
            self._save(room)
            return room

    @staticmethod
    def _contract_candidates(room, contract_round):
        rng = random.Random(room['seed'] + 1009 * contract_round)
        colors = rng.sample(CONTRACT_COLORS, 3)
        return [rng.choice([key for key, value in COLOR_CONTRACTS.items()
                            if value['color'] == color]) for color in colors]

    def record_boss_victory(self, room_id, actor_id, *, sealed_rewards=None, now=None):
        """Advance one completed painting and open a vote at each stage seal."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] != 'running':
                raise PaintedMazeError('目前不能推進下一幅畫作。')
            if actor_id not in room['members']:
                raise PaintedMazeError('只有隊伍成員可以推進繪境迷廊。')
            if room['boss_index'] >= len(room['paintings']):
                raise PaintedMazeError('前置畫作已全部完成。')
            room['boss_index'] += 1
            room['rest_ready'] = []
            if sealed_rewards:
                room['sealed_rewards'].extend(sealed_rewards)
            if room['boss_index'] % paintings_per_stage(room) == 0:
                contract_round = room['boss_index'] // paintings_per_stage(room)
                room.setdefault('reward_due', []).append({
                    'kind': 'stage', 'checkpoint': contract_round})
                room['stage'] = contract_round
                room['status'] = 'contract'
                room['contract_vote'] = {
                    'round': contract_round,
                    'candidates': self._contract_candidates(room, contract_round),
                    'votes': {},
                    'opened_at': now,
                    'deadline': now + CONTRACT_VOTE_SECONDS,
                }
            self._save(room)
            return room

    def cast_contract_vote(self, room_id, user_id, contract_id, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            vote = room.get('contract_vote') if room else None
            if not room or room['status'] != 'contract' or not vote:
                raise PaintedMazeError('目前不是色彩契約投票階段。')
            if user_id not in room['members']:
                raise PaintedMazeError('只有隊伍成員可以投票。')
            if now >= vote['deadline']:
                raise PaintedMazeError('色彩契約投票已截止。')
            if contract_id not in vote['candidates']:
                raise PaintedMazeError('這份契約不在本次候選中。')
            vote['votes'][str(user_id)] = contract_id
            self._save(room)
            return room

    def resolve_contract(self, room_id, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            vote = room.get('contract_vote') if room else None
            if not room or room['status'] != 'contract' or not vote:
                raise PaintedMazeError('目前沒有待結算的色彩契約。')
            if now < vote['deadline']:
                raise PaintedMazeError('色彩契約投票尚未截止。')
            counts = {candidate: 0 for candidate in vote['candidates']}
            for choice in vote['votes'].values():
                if choice in counts:
                    counts[choice] += 1
            highest = max(counts.values())
            tied = [candidate for candidate in vote['candidates'] if counts[candidate] == highest]
            rng = random.Random(room['seed'] + 1009 * vote['round'] + 17)
            selected = rng.choice(tied)
            room['contracts'].append(selected)
            vote['result'] = selected
            vote['resolved_at'] = now
            room['last_contract_vote'] = vote
            room['contract_vote'] = None
            room['status'] = 'running'
            self._save(room)
            return room

    def start_battle(self, room_id, actor_id, battle, *, deadline, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] != 'running' or room.get('battle'):
                raise PaintedMazeError('戰鬥已開始或目前不能挑戰。')
            if actor_id not in room['members'] or now >= room['expires_at']:
                raise PaintedMazeError('無法開始這場戰鬥。')
            if room['boss_index'] == len(room['paintings']):
                if room.get('final_vote', {}).get('result') != 'enter':
                    raise PaintedMazeError('必須先由全隊投票決定進入尾王。')
                room['final_entered'] = True
            if set(room.get('rest_ready', ())) != set(room['members']):
                raise PaintedMazeError('請等待全隊在休息點確認準備完成。')
            room.update(battle=battle, battle_deadline=deadline)
            self._save(room)
            return room

    def rest_participant(self, room_id, user_id, *, expected_index=None, now=None):
        now = time.time() if now is None else now
        room = self.get(room_id)
        if (not room or room['status'] != 'running' or room.get('battle')
                or room.get('final_vote') or now >= room['expires_at']):
            raise PaintedMazeError('目前不是可調整技能的休息點。')
        if expected_index is not None and room['boss_index'] != expected_index:
            raise PaintedMazeError('這個休息點已結束，請重新開啟技能面板。')
        participant = next((p for p in room['participants'] if p['id'] == user_id), None)
        if participant is None:
            raise PaintedMazeError('只有隊員可以使用休息點。')
        return room, participant

    def save_rest_tactics(self, room_id, user_id, rules, passive_id, *, expected_index):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room, participant = self.rest_participant(room_id, user_id, expected_index=expected_index)
            participant['rules'] = rules
            participant['passive_id'] = passive_id
            room['rest_ready'] = [uid for uid in room.get('rest_ready', []) if uid != user_id]
            self._save(room)
            return room

    def ready_at_rest(self, room_id, user_id, *, expected_index):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room, _ = self.rest_participant(room_id, user_id, expected_index=expected_index)
            ready = set(room.get('rest_ready', []))
            if user_id in ready:
                ready.remove(user_id)
            else:
                ready.add(user_id)
            room['rest_ready'] = sorted(ready)
            self._save(room)
            return room

    def save_battle(self, room_id, battle, *, expected_round, deadline):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if (not room or room['status'] != 'running' or not room.get('battle')
                    or room['battle']['round'] != expected_round):
                raise PaintedMazeError('戰鬥回合已更新，請重新開啟面板。')
            room.update(battle=battle, battle_deadline=deadline)
            self._save(room)
            return room

    def vote_final(self, room_id, user_id, choice, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            vote = room.get('final_vote') if room else None
            if (not room or room['status'] != 'running' or room.get('battle')
                    or not vote or vote.get('result') or now >= vote['deadline']):
                raise PaintedMazeError('尾王去留投票已截止或尚未開啟。')
            if user_id not in room['members'] or choice not in ('enter', 'retreat'):
                raise PaintedMazeError('只有隊員可以投票挑戰或撤退。')
            vote['votes'][str(user_id)] = choice
            self._save(room)
            return room

    def ensure_final_vote(self, room_id, *, now=None):
        """Give saved rooms already waiting at the final door the new vote."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if (room and room['status'] == 'running' and room['boss_index'] == len(room['paintings'])
                    and not room.get('battle') and not room.get('final_vote')):
                if set(room.get('rest_ready', [])) != set(room['members']):
                    raise PaintedMazeError('請等待全隊在休息點確認準備完成。')
                room['final_vote'] = {'votes': {}, 'deadline': now + 60, 'result': None}
                self._save(room)
            return room

    def resolve_final_vote(self, room_id, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            vote = room.get('final_vote') if room else None
            if not vote or vote.get('result') or room['status'] != 'running':
                return room
            if now < vote['deadline']:
                raise PaintedMazeError('尾王去留投票尚未截止。')
            entering = sum(vote['votes'].get(str(uid)) == 'enter' for uid in room['members'])
            vote['result'] = 'enter' if entering > len(room['members']) / 2 else 'retreat'
            if vote['result'] == 'retreat':
                self._finish(room, 'retreated', None, '全隊投票撤退，保留全部累積掉落', now)
            self._save(room)
            return room

    def resolve_contracts_due(self, *, now=None):
        now = time.time() if now is None else now
        due = [room['id'] for room in self.active()
               if room['status'] == 'contract'
               and room.get('contract_vote', {}).get('deadline', now + 1) <= now]
        return [self.resolve_contract(room_id, now=now) for room_id in due]

    @staticmethod
    def _finish(room, status, actor_id, reason, now):
        room.update(status=status, ended_at=now, ended_by=actor_id, end_reason=reason)
        room['terminal_refresh_pending'] = True
        if room.get('reward_policy') == 'escrow_v2':
            room['loot_percent'] = 50 if room.get('final_entered') and status != 'completed' else 100
            if room['loot_percent'] == 50:
                room['end_reason'] += '；本場累積掉落減半（結晶保留數向上取整）'

    def close(self, room_id, actor_id, *, administrator=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room or room['status'] not in ACTIVE_STATUSES:
                raise PaintedMazeError('這個房間已經結束。')
            if not administrator and (room['status'] != 'lobby' or actor_id != room['host_id']):
                raise PaintedMazeError('只有房主能關閉大廳；進行中的房間只能由管理員強制結束。')
            if administrator:
                status, reason = 'admin_ended', '管理員強制結束'
            else:
                status, reason = 'cancelled', '房主關閉大廳'
            self._finish(room, status, actor_id, reason, now)
            self._save(room)
            return room

    def expire_due(self, *, now=None):
        now = time.time() if now is None else now
        expired = []
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = self.db.execute('''SELECT data FROM rpg_painted_maze_rooms
                WHERE status IN ('lobby','running','contract') AND expires_at<=?''',
                (now,)).fetchall()
            for row in rows:
                room = json.loads(row[0])
                self._finish(room, 'expired', None, '流程逾期', now)
                self._save(room)
                expired.append(room)
        return expired

    def rooms_with_rewards_due(self):
        rows = self.db.execute("SELECT data FROM rpg_painted_maze_rooms").fetchall()
        return [room for row in rows if (room := json.loads(row[0])).get('reward_due')
                and (room.get('reward_policy') != 'escrow_v2' or room['status'] not in ACTIVE_STATUSES)]

    def terminal_refreshes(self):
        rows = self.db.execute('SELECT data FROM rpg_painted_maze_rooms').fetchall()
        return [room for row in rows if (room := json.loads(row[0])).get('terminal_refresh_pending')]

    def mark_terminal_refreshed(self, room_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if room:
                room.pop('terminal_refresh_pending', None)
                self._save(room)

    def mark_reward_complete(self, room_id, kind, checkpoint, *, now=None):
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.get(room_id)
            if not room:
                raise PaintedMazeError('找不到繪境迷廊房間。')
            room['reward_due'] = [item for item in room.get('reward_due', [])
                                  if not (item.get('kind') == kind
                                          and item.get('checkpoint') == checkpoint)]
            room.setdefault('sealed_rewards', []).append({
                'kind': f'{kind}_complete', 'checkpoint': checkpoint, 'sealed_at': now})
            self._save(room)
            return room
