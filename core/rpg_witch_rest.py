"""Witch Rest Ritual equipment, reward rolls, and persistent player progress."""
from collections import Counter
from dataclasses import dataclass
import json
import random
import time
import uuid

from core.rpg import record_gold
from core.rpg_character import CharacterError, Item, ITEMS, add_owned_item
from core.rpg_alchemy import CORE_ITEM


MIN_LEVEL = 70
MAX_ENRAGE = 4_000
ENTRY_PROOFS = 10
REWARD_VERSION = 1


@dataclass(frozen=True)
class Witch:
    id: str
    name: str
    accessory: str
    weapon_effect: str
    suit_effect: str
    titles: tuple[str, str, str]


WITCHES = {
    'ema': Witch('ema', '櫻羽艾瑪', '斷罪花冠', '魔女殺手', '絕不放棄',
                 ('斷罪之花的見證者', '不被厭棄之人', '魔女殺手的共犯')),
    'hiro': Witch('hiro', '二階堂希羅', '黎明時計', '黎明回刻', '昨日殘像',
                  ('正義的追問者', '輪迴黎明的見證者', '導正世界之人')),
}

JOBS = {
    '裝甲步兵': ('infantry', '戰斧', '步兵甲'),
    '騎士': ('knight', '劍盾', '騎士鎧'),
    '弓兵': ('archer', '長弓', '獵裝'),
    '僧侶': ('monk', '權杖', '僧袍'),
}

BASE_COMBAT = {
    80: {
        '裝甲步兵': ((179, 225, 36, 0), (713, 37, 149, 0)),
        '騎士': ((339, 177, 77, 0), (813, 0, 185, 0)),
        '弓兵': ((0, 170, 0, 0), (634, 49, 107, 0)),
        '僧侶': ((0, 177, 0, 188), (634, 0, 107, 152)),
    },
    90: {
        '裝甲步兵': ((219, 276, 44, 0), (872, 46, 181, 0)),
        '騎士': ((416, 217, 95, 0), (998, 0, 227, 0)),
        '弓兵': ((0, 208, 0, 0), (769, 60, 128, 0)),
        '僧侶': ((0, 217, 0, 231), (769, 0, 128, 187)),
    },
}

EFFECT_TEXT = {
    'ema': {
        'weapon': '【魔女殺手】對行動開始時 HP 不高於 35% 的敵人，直接傷害 +24%。',
        'suit': '【絕不放棄】每場一次，致命傷害後保留 1 HP，並至下次行動結束前受到傷害 -25%。',
        'accessory': '對 HP 低於 30% 的敵人，直接傷害 +8%。',
    },
    'hiro': {
        'weapon': '【黎明回刻】每施放第二個主動技能，另外兩個冷卻技能各縮短 1 回合，每場最多六次。',
        'suit': '【昨日殘像】每場一次，HP 首次降至 40% 以下時回復至上回合結束值（上限 15%），並解除一個可淨化負面狀態。',
        'accessory': '每場一次，HP 高於 35% 時受到致命直接傷害，以 1 HP 存活。',
    },
}


def equipment_id(witch_id, job, slot, tier=80):
    slug = JOBS[job][0]
    return f'witch_rest:{witch_id}:t{tier}:{slug}:{slot}'


def _register_items():
    materials = {
        'witch_rest:fragment': Item('魔女殘片', '', '', 0, (0,) * 5, category='製作材料', description='魔女裝備詞條重鑄材料。'),
        'witch_rest:dust': Item('詞條重鑄粉塵', '', '', 0, (0,) * 5, category='製作材料', description='用於重鑄指定的一般詞條。'),
        'witch_rest:crystal_shard': Item('凝聚魔女結晶碎片', '', '', 0, (0,) * 5, category='製作材料', description='50 個可合成一顆凝聚魔女結晶。'),
        'witch_rest:crystal': Item('凝聚魔女結晶', '', '', 0, (0,) * 5, category='製作材料', description='T80 魔女裝備昇階至 T90 的催化材料。'),
        'witch_rest:advanced_memory': Item('高級詞條記憶', '', '', 0, (0,) * 5, category='製作材料', description='嘗試將指定詞條提高一級。', transferable=False),
        'witch_rest:memory_page': Item('高級詞條記憶殘頁', '', '', 0, (0,) * 5, category='製作材料', description='2 張可合成一個高級詞條記憶。'),
    }
    ITEMS.update(materials)
    for witch_id, witch in WITCHES.items():
        ITEMS[f'witch_rest:{witch_id}:core'] = Item(
            f'{witch.name}魔力核心', '', '', 0, (0,) * 5, category='製作材料',
            description=f'用於昇階{witch.name}的魔女裝備。')
        ITEMS[f'witch_rest:{witch_id}:directed_memory'] = Item(
            f'{witch.name}・定向詞條記憶', '', '', 0, (0,) * 5, category='製作材料',
            description=f'可將{witch.name}裝備的指定詞條改為同部位合法種類。')
        ITEMS[f'witch_rest:{witch_id}:accessory'] = Item(
            witch.accessory, '飾品', '', 4, (0,) * 5, required_level=80,
            description=EFFECT_TEXT[witch_id]['accessory'], sell_price=10_000, embroidery_slots=3)
        for tier in (80, 90):
            for job, (_, weapon_name, suit_name) in JOBS.items():
                for index, (slot, suffix) in enumerate((('weapon', weapon_name), ('suit', suit_name))):
                    chinese_slot = '武器' if slot == 'weapon' else '套裝'
                    ITEMS[equipment_id(witch_id, job, slot, tier)] = Item(
                        f'{witch.name}・{suffix}', chinese_slot, job, 4 if tier == 80 else 5, (0,) * 5,
                        BASE_COMBAT[tier][job][index], (80, 120) if slot == 'weapon' else (100, 100),
                        required_level=tier, speed=(15 if tier == 80 else 16) if slot == 'weapon' else 0,
                        accuracy=(80 if tier == 80 else 90) if slot == 'weapon' else 0,
                        description=EFFECT_TEXT[witch_id][slot], sell_price=10_000 if tier == 80 else 12_000)


_register_items()


POINTS = ((0, 12), (100, 16), (250, 21), (500, 27), (750, 34), (1000, 42), (2000, 48), (4000, 54))
TREASURE_CHANCE = ((0, 2), (100, 6.5), (250, 8), (500, 10), (750, 12.5), (1000, 15), (2000, 17.5), (4000, 20))
LUCK_GAIN = ((100, 1), (250, 2), (500, 3), (750, 4), (1000, 5), (2000, 6), (4000, 8))
GRADE_WEIGHTS = (
    (0, (80, 20, 0, 0)), (100, (50, 45, 5, 0)), (250, (25, 60, 15, 0)),
    (500, (0, 55, 40, 5)), (1000, (0, 25, 60, 15)),
    (2000, (0, 0, 65, 35)), (4000, (0, 0, 40, 60)),
)


def bracket_value(table, enrage):
    if type(enrage) is not int or not 0 <= enrage <= MAX_ENRAGE:
        raise CharacterError('魔女化必須是 0～4,000% 的整數。')
    return next(value for floor, value in reversed(table) if enrage >= floor)


def party_size_allowed(enrage, size):
    return 1 <= size <= 6 if enrage < 100 else 3 <= size <= 6


def reroll_cost(previous_rolls):
    if type(previous_rolls) is not int or previous_rolls < 0:
        raise CharacterError('重鑄次數無效。')
    return {'fragment': 4 + 2 * previous_rolls, 'dust': 2 + previous_rolls,
            'gold': 1_500 * (previous_rolls + 1)}


def _weighted_choice(rng, values):
    return rng.choices([key for key, _ in values], weights=[weight for _, weight in values], k=1)[0]


def affix_kinds(job, slot, index):
    if job not in JOBS or slot not in ('weapon', 'suit'):
        raise CharacterError('無效的魔女裝備。')
    monk = job == '僧侶'
    prefix = ((('vitality', 30), ('assault', 20), ('fortitude', 30), ('prayer', 20)) if monk and slot == 'weapon'
              else (('vitality', 35), ('assault', 10), ('fortitude', 40), ('prayer', 15)) if monk
              else (('vitality', 35), ('assault', 25), ('fortitude', 40)) if slot == 'weapon'
              else (('vitality', 40), ('assault', 15), ('fortitude', 45)))
    suffix = ((('precision', 10), ('haste', 25), ('critical', 25), ('prowess', 15), ('stability', 15), ('drain', 10))
              if slot == 'weapon' else
              (('evasion', 15), ('revival', 25), ('guard', 15), ('corrosion', 25), ('unyielding', 20)))
    return (prefix, suffix)[index]


def roll_affixes(rng, job, slot, enrage):
    prefix, suffix = affix_kinds(job, slot, 0), affix_kinds(job, slot, 1)
    grades = tuple(zip(range(1, 5), bracket_value(GRADE_WEIGHTS, enrage)))
    return ((_weighted_choice(rng, prefix), _weighted_choice(rng, grades)),
            (_weighted_choice(rng, suffix), _weighted_choice(rng, grades)))


def treasure_weights(enrage):
    if enrage < 100:
        return (('weapon', 35), ('suit', 35), ('accessory', 25), ('advanced_memory', 5))
    if enrage < 250:
        return (('weapon', 27.5), ('suit', 27.5), ('accessory', 15), ('crystal', 25), ('advanced_memory', 5))
    if enrage < 500:
        return (('weapon', 25), ('suit', 25), ('accessory', 15), ('crystal', 25), ('advanced_memory', 10))
    if enrage < 750:
        return (('weapon', 22.5), ('suit', 22.5), ('accessory', 12.5), ('crystal', 27.5), ('advanced_memory', 15))
    if enrage < 1000:
        return (('weapon', 20), ('suit', 20), ('accessory', 10), ('crystal', 30), ('advanced_memory', 20))
    return (('weapon', 15), ('suit', 15), ('accessory', 5), ('crystal', 20), ('directed_memory', 25), ('advanced_memory', 20))


def roll_reward(rng, witch_id, job, enrage, dry_wins=0, luck_points=0, missing_slot=None):
    """Roll one already-won personal reward without mutating storage."""
    if witch_id not in WITCHES or job not in JOBS:
        raise CharacterError('無效的魔女或職業。')
    budget = bracket_value(POINTS, enrage)
    rewards, gold = Counter(), 0
    pool = [('fragment', 4, 28), ('core', 10, 8), ('dust', 6, 25),
            ('gold', 6, 24), ('food', 4, 10)]
    if enrage >= 100:
        pool.append(('crystal_shard', 15, 5))
    while True:
        legal = [(kind, cost, weight) for kind, cost, weight in pool if cost <= budget]
        if not legal:
            break
        kind, cost, _ = _weighted_choice(rng, [((kind, cost, weight), weight) for kind, cost, weight in legal])
        budget -= cost
        if kind == 'gold':
            gold += 250
        elif kind == 'food':
            rewards['fishing:temple:common'] += 1
        else:
            key = f'witch_rest:{witch_id}:core' if kind == 'core' else f'witch_rest:{kind}'
            rewards[key] += 1
    base = bracket_value(TREASURE_CHANCE, enrage)
    chance = base if enrage < 100 else min(30, base + luck_points * .5)
    treasure = None
    affixes = None
    if rng.random() * 100 < chance:
        kind = _weighted_choice(rng, treasure_weights(enrage))
        if kind in ('weapon', 'suit'):
            kind = missing_slot or kind
            treasure = equipment_id(witch_id, job, kind)
            affixes = roll_affixes(rng, job, kind, enrage)
        else:
            treasure = {
                'accessory': f'witch_rest:{witch_id}:accessory',
                'crystal': 'witch_rest:crystal',
                'directed_memory': f'witch_rest:{witch_id}:directed_memory',
                'advanced_memory': 'witch_rest:advanced_memory',
            }[kind]
    next_dry, next_luck = dry_wins, luck_points
    if enrage >= 100:
        if treasure:
            next_dry = next_luck = 0
        else:
            next_dry += 1
            if next_dry > 8:
                next_luck += bracket_value(LUCK_GAIN, enrage)
    return {'points': bracket_value(POINTS, enrage), 'items': dict(rewards), 'gold': gold,
            'treasure': treasure, 'affixes': affixes, 'treasure_chance': chance,
            'dry_wins': next_dry, 'luck_points': next_luck}


class WitchRestStore:
    """Atomic reward delivery and persistent per-witch progress."""
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_progress (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, witch_id TEXT NOT NULL,
                highest_enrage INTEGER NOT NULL DEFAULT -1, total_wins INTEGER NOT NULL DEFAULT 0,
                eligible_wins INTEGER NOT NULL DEFAULT 0, dry_wins INTEGER NOT NULL DEFAULT 0,
                luck_points INTEGER NOT NULL DEFAULT 0, found_weapon INTEGER NOT NULL DEFAULT 0,
                found_suit INTEGER NOT NULL DEFAULT 0, found_accessory INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id,witch_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_rewards (
                clear_id TEXT NOT NULL, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                witch_id TEXT NOT NULL, enrage INTEGER NOT NULL, data TEXT NOT NULL,
                delivered_at REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(clear_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_equipment (
                instance_id INTEGER PRIMARY KEY, witch_id TEXT NOT NULL,
                prefix_rerolls INTEGER NOT NULL DEFAULT 0 CHECK(prefix_rerolls>=0),
                suffix_rerolls INTEGER NOT NULL DEFAULT 0 CHECK(suffix_rerolls>=0),
                source_clear_id TEXT NOT NULL, source_reward_index INTEGER NOT NULL,
                UNIQUE(source_clear_id,source_reward_index))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_entry_receipts (
                room_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY(room_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_craft_receipts (
                request_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                operation TEXT NOT NULL, data TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_upgrade_receipts (
                instance_id INTEGER NOT NULL, target_tier INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(instance_id,target_tier))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_reroll_receipts (
                request_id TEXT PRIMARY KEY, instance_id INTEGER NOT NULL,
                decided INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_processing_receipts (
                request_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                operation TEXT NOT NULL, data TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_rooms (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, status TEXT NOT NULL,
                expires_at REAL NOT NULL, data TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_rest_room_numbers (
                guild_id INTEGER PRIMARY KEY, next_number INTEGER NOT NULL)''')

    def _take_item(self, guild_id, user_id, item_id, quantity):
        changed = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
            WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=?''',
            (quantity, guild_id, user_id, item_id, quantity))
        if not changed.rowcount:
            raise CharacterError(f'{ITEMS[item_id].name}不足，需要 {quantity} 個。')
        self.db.execute('''DELETE FROM rpg_inventory
            WHERE guild_id=? AND user_id=? AND item_id=? AND quantity=0''',
            (guild_id, user_id, item_id))

    def _take_gold(self, guild_id, user_id, amount, source, reference):
        changed = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
            WHERE guild_id=? AND user_id=? AND gold>=?''', (amount, guild_id, user_id, amount))
        if not changed.rowcount:
            raise CharacterError(f'金幣不足，需要 {amount:,} 金幣。')
        record_gold(self.db, guild_id, user_id, -amount, source, reference)

    def charge_entry(self, room_id, guild_id, user_ids, *, practice=False):
        """Charge every participant together; one shortage rolls the whole start back."""
        if self.db.in_transaction:
            return self._charge_entry(room_id, guild_id, user_ids, practice=practice)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return self._charge_entry(room_id, guild_id, user_ids, practice=practice)

    def _charge_entry(self, room_id, guild_id, user_ids, *, practice=False):
        user_ids = tuple(dict.fromkeys(user_ids))
        if not user_ids:
            raise CharacterError('隊伍中沒有可參戰的玩家。')
        existing = self.db.execute(
            'SELECT user_id FROM rpg_witch_rest_entry_receipts WHERE room_id=?',
            (room_id,)).fetchall()
        if existing:
            if {row[0] for row in existing} != set(user_ids):
                raise CharacterError('這個房間的開戰名單與已收費紀錄不符。')
            return False
        if not practice:
            for user_id in user_ids:
                self._take_item(guild_id, user_id, 'proof:raid', ENTRY_PROOFS)
        self.db.executemany('INSERT INTO rpg_witch_rest_entry_receipts VALUES (?,?)',
                            [(room_id, user_id) for user_id in user_ids])
        return True

    @staticmethod
    def _decode(row):
        return json.loads(row[0]) if row else None

    def room(self, room_id):
        return self._decode(self.db.execute(
            'SELECT data FROM rpg_witch_rest_rooms WHERE id=?', (room_id,)).fetchone())

    get = room

    def active_rooms(self, guild_id=None):
        query = "SELECT data FROM rpg_witch_rest_rooms WHERE status IN ('lobby','running')"
        args = ()
        if guild_id is not None:
            query += ' AND guild_id=?'
            args = (guild_id,)
        return [json.loads(row[0]) for row in self.db.execute(query, args)]

    def _save_room(self, room):
        self.db.execute('''UPDATE rpg_witch_rest_rooms SET status=?,expires_at=?,data=? WHERE id=?''',
                        (room['status'], room['expires_at'], json.dumps(room, ensure_ascii=False), room['id']))

    def save(self, room):
        with self.db:
            self._save_room(room)
        return room

    def create_room(self, guild_id, host_id, witch_id, enrage, practice=False, *, now=None):
        bracket_value(POINTS, enrage)
        if witch_id not in WITCHES:
            raise CharacterError('尚未開放這位魔女。')
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if any(host_id in room['members'] for room in self.active_rooms(guild_id)):
                raise CharacterError('你已在另一個魔女安息儀式房間中。')
            row = self.db.execute('SELECT next_number FROM rpg_witch_rest_room_numbers WHERE guild_id=?',
                                  (guild_id,)).fetchone()
            number = row[0] if row else 1
            self.db.execute('''INSERT INTO rpg_witch_rest_room_numbers VALUES (?,?)
                ON CONFLICT(guild_id) DO UPDATE SET next_number=excluded.next_number''',
                (guild_id, number + 1))
            room = {'id': uuid.uuid4().hex, 'guild_id': guild_id, 'host_id': host_id,
                    'witch_id': witch_id, 'enrage': enrage, 'practice': bool(practice),
                    'number': number, 'status': 'lobby', 'members': [host_id], 'participants': [],
                    'channel_id': None, 'message_id': None, 'created_at': now,
                    'expires_at': now + 1_800, 'battle': None, 'result': None}
            self.db.execute('INSERT INTO rpg_witch_rest_rooms VALUES (?,?,?,?,?)',
                            (room['id'], guild_id, 'lobby', room['expires_at'],
                             json.dumps(room, ensure_ascii=False)))
            return room

    def attach_room(self, room_id, channel_id, message_id, *, parent_channel_id=None,
                    index_message_id=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.room(room_id)
            if not room or room['status'] != 'lobby':
                raise CharacterError('這個房間已經關閉。')
            room.update(channel_id=channel_id, message_id=message_id,
                        parent_channel_id=parent_channel_id,
                        index_message_id=index_message_id)
            self._save_room(room)
            return room

    def change_member(self, room_id, user_id, level, *, leave=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.room(room_id)
            if not room or room['status'] != 'lobby' or now >= room['expires_at']:
                raise CharacterError('房間已經開始、關閉或逾期。')
            if leave:
                if user_id == room['host_id']:
                    raise CharacterError('房主不能退出自己的隊伍。')
                if user_id not in room['members']:
                    raise CharacterError('你尚未加入這個隊伍。')
                room['members'].remove(user_id)
            else:
                if level < MIN_LEVEL:
                    raise CharacterError(f'魔女安息儀式需要 Lv.{MIN_LEVEL}。')
                if user_id in room['members']:
                    raise CharacterError('你已經在隊伍中。')
                if len(room['members']) >= 6:
                    raise CharacterError('隊伍已滿。')
                if any(user_id in other['members'] for other in self.active_rooms(room['guild_id'])
                       if other['id'] != room_id):
                    raise CharacterError('你已在另一個魔女安息儀式房間中。')
                room['members'].append(user_id)
            room['expires_at'] = now + 1_800
            self._save_room(room)
            return room

    def start_room(self, room_id, actor_id, participants, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.room(room_id)
            if not room or room['status'] != 'lobby' or now >= room['expires_at']:
                raise CharacterError('房間已經開始、關閉或逾期。')
            if actor_id != room['host_id']:
                raise CharacterError('只有房主可以開始戰鬥。')
            if [participant['id'] for participant in participants] != room['members']:
                raise CharacterError('隊伍快照不完整。')
            if not party_size_allowed(room['enrage'], len(participants)):
                requirement = '1～6' if room['enrage'] < 100 else '3～6'
                raise CharacterError(f'這個魔女化需要 {requirement} 人。')
            if any(participant['state']['level'] < MIN_LEVEL for participant in participants):
                raise CharacterError(f'所有隊員都必須達到 Lv.{MIN_LEVEL}。')
            self._charge_entry(room_id, room['guild_id'], room['members'], practice=room['practice'])
            room.update(status='running', participants=participants, started_at=now,
                        expires_at=now + 7_200)
            self._save_room(room)
            return room

    def finish_room(self, room_id, result, battle_summary, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.room(room_id)
            if not room or room['status'] != 'running':
                raise CharacterError('這場戰鬥已經結算。')
            archive_at = now
            room.update(status='completed', result=result, battle=battle_summary,
                        finished_at=now, archive_at=archive_at, expires_at=archive_at,
                        thread_archived=False)
            self._save_room(room)
            return room

    def archives_due(self, *, now=None):
        rows = self.db.execute(
            "SELECT data FROM rpg_witch_rest_rooms "
            "WHERE status IN ('completed','cancelled','expired')").fetchall()
        return [room for row in rows if not (room := json.loads(row[0])).get('thread_deleted')]

    def pending_reports(self):
        rows = self.db.execute(
            "SELECT data FROM rpg_witch_rest_rooms WHERE status='completed'").fetchall()
        return [room for row in rows if not (room := json.loads(row[0])).get('report_message_id')]

    def rooms_missing_rewards(self):
        rows = self.db.execute(
            "SELECT data FROM rpg_witch_rest_rooms WHERE status='completed'").fetchall()
        missing = []
        for row in rows:
            room = json.loads(row[0])
            if room.get('result') != '勝利' or room.get('practice'):
                continue
            participant_ids = {participant['id'] for participant in room.get('participants', ())}
            rewarded_ids = {rewarded[0] for rewarded in self.db.execute(
                'SELECT user_id FROM rpg_witch_rest_rewards WHERE clear_id=?',
                (room['id'],)).fetchall()}
            if participant_ids - rewarded_ids:
                missing.append(room)
        return missing

    def mark_archived(self, room_id):
        with self.db:
            room = self.room(room_id)
            if room and room['status'] in ('completed', 'cancelled', 'expired'):
                room['thread_archived'] = True
                room['thread_deleted'] = True
                self._save_room(room)
            return room

    def close_room(self, room_id, actor_id, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            room = self.room(room_id)
            if not room or room['status'] != 'lobby' or actor_id != room['host_id']:
                raise CharacterError('只有房主能關閉尚未開戰的房間。')
            room.update(status='cancelled', expires_at=now, thread_archived=False)
            self._save_room(room)
            return room

    def expire_rooms(self, *, now=None):
        now = time.time() if now is None else now
        expired = []
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for room in self.active_rooms():
                if room['expires_at'] <= now:
                    room.update(status='expired', expires_at=now, thread_archived=False)
                    self._save_room(room)
                    expired.append(room)
        return expired

    def craft_crystal(self, request_id, guild_id, user_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_craft_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            self._take_item(guild_id, user_id, 'witch_rest:crystal_shard', 50)
            self._take_gold(guild_id, user_id, 5_000, 'witch_rest_crystal_craft', request_id)
            add_owned_item(self.db, guild_id, user_id, 'witch_rest:crystal')
            result = {'item_id': 'witch_rest:crystal', 'quantity': 1, 'gold': 5_000}
            self.db.execute('INSERT INTO rpg_witch_rest_craft_receipts VALUES (?,?,?,?,?)',
                            (request_id, guild_id, user_id, 'crystal', json.dumps(result)))
            return result

    def compose_memory(self, request_id, guild_id, user_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_craft_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            self._take_item(guild_id, user_id, 'witch_rest:memory_page', 2)
            add_owned_item(self.db, guild_id, user_id, 'witch_rest:advanced_memory')
            result = {'item_id': 'witch_rest:advanced_memory', 'quantity': 1}
            self.db.execute('INSERT INTO rpg_witch_rest_craft_receipts VALUES (?,?,?,?,?)',
                            (request_id, guild_id, user_id, 'memory', json.dumps(result)))
            return result

    def _equipment_affix(self, guild_id, user_id, instance_id, affix_index):
        if affix_index not in (0, 1):
            raise CharacterError('請選擇前綴或後綴。')
        row = self.db.execute('''SELECT e.item_id,m.witch_id,m.prefix_rerolls,m.suffix_rerolls,
            a.affix_id,a.effect_key,a.rolled_value FROM rpg_equipment_instances e
            JOIN rpg_witch_rest_equipment m ON m.instance_id=e.instance_id
            JOIN rpg_instance_affixes a ON a.instance_id=e.instance_id AND a.affix_index=?
            WHERE e.instance_id=? AND e.guild_id=? AND e.user_id=?''',
            (affix_index, instance_id, guild_id, user_id)).fetchone()
        if not row:
            raise CharacterError('找不到這件魔女裝備或指定詞條。')
        item_id, witch_id, prefix_rolls, suffix_rolls, affix_id, effect_key, value = row
        _, _, _, slug, slot = item_id.split(':')
        job = next(name for name, data in JOBS.items() if data[0] == slug)
        kind, grade = affix_id.split(':')[1:]
        return dict(item_id=item_id, witch_id=witch_id, job=job, slot=slot,
                    kind=kind, grade=int(grade), effect_key=effect_key, value=value,
                    rolls=(prefix_rolls, suffix_rolls)[affix_index])

    @staticmethod
    def _affix_effect(index, kind):
        maps = ({'vitality': 'combat:0', 'assault': 'combat:1', 'fortitude': 'combat:2', 'prayer': 'combat:3'},
                {'precision': 'accuracy', 'haste': 'speed', 'critical': 'critical_points',
                 'prowess': 'critical_damage_percent_add', 'stability': 'stability_lower',
                 'drain': 'lifesteal', 'evasion': 'evasion', 'revival': 'healing_received_percent',
                 'guard': 'direct_damage_reduction', 'corrosion': 'dot_damage_reduction',
                 'unyielding': 'low_hp_damage_reduction'})
        try:
            return maps[index][kind]
        except KeyError as exc:
            raise CharacterError('這個部位不能使用指定詞條。') from exc

    @staticmethod
    def _affix_value(item_id, job, index, kind, grade):
        tier = int(item_id.split(':')[2][1:])
        if index == 0:
            combat_index = ('vitality', 'assault', 'fortitude', 'prayer').index(kind)
            total = sum(piece[combat_index] for piece in BASE_COMBAT[tier][job])
            divisor = .35 if tier == 80 else .375
            return round(total / divisor * (1 + grade) / 100)
        values = {
            'precision': (2, 4, 6, 8), 'haste': (1, 2, 3, 4),
            'critical': (1, 2, 3, 4), 'prowess': (2, 4, 6, 8),
            'stability': (2, 4, 6, 8), 'drain': (1, 2, 3, 4),
            'evasion': (1, 2, 3, 4), 'revival': (3, 5, 7, 10),
            'guard': (1, 2, 3, 4), 'corrosion': (5, 10, 15, 20),
            'unyielding': (3, 5, 7, 10),
        }
        return values[kind][grade - 1]

    def equipment(self, guild_id, user_id):
        rows = self.db.execute('''SELECT e.instance_id,e.item_id,m.witch_id,m.prefix_rerolls,m.suffix_rerolls
            FROM rpg_equipment_instances e JOIN rpg_witch_rest_equipment m ON m.instance_id=e.instance_id
            WHERE e.guild_id=? AND e.user_id=? ORDER BY e.instance_id DESC''',
            (guild_id, user_id)).fetchall()
        result = []
        for instance_id, item_id, witch_id, prefix_rolls, suffix_rolls in rows:
            affixes = self.db.execute('''SELECT affix_index,affix_id,rolled_value
                FROM rpg_instance_affixes WHERE instance_id=? ORDER BY affix_index''',
                (instance_id,)).fetchall()
            result.append(dict(instance_id=instance_id, item_id=item_id, witch_id=witch_id,
                               rerolls=(prefix_rolls, suffix_rolls), affixes=affixes))
        return result

    def offer_reroll(self, request_id, guild_id, user_id, instance_id, affix_index):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_reroll_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            current = self._equipment_affix(guild_id, user_id, instance_id, affix_index)
            cost = reroll_cost(current['rolls'])
            self._take_item(guild_id, user_id, 'witch_rest:fragment', cost['fragment'])
            self._take_item(guild_id, user_id, 'witch_rest:dust', cost['dust'])
            self._take_gold(guild_id, user_id, cost['gold'], 'witch_rest_reroll', request_id)
            rng = random.Random(request_id)
            kind = current['kind']
            while kind == current['kind']:
                kind = roll_affixes(rng, current['job'], current['slot'], 0)[affix_index][0]
            effect = self._affix_effect(affix_index, kind)
            value = self._affix_value(current['item_id'], current['job'], affix_index, kind, current['grade'])
            column = 'prefix_rerolls' if affix_index == 0 else 'suffix_rerolls'
            self.db.execute(f'UPDATE rpg_witch_rest_equipment SET {column}={column}+1 WHERE instance_id=?',
                            (instance_id,))
            result = dict(instance_id=instance_id, affix_index=affix_index,
                          old=[current['kind'], current['grade'], current['value']],
                          new=[kind, current['grade'], value], effect_key=effect, cost=cost, decided=False)
            self.db.execute('INSERT INTO rpg_witch_rest_reroll_receipts VALUES (?,?,0,?)',
                            (request_id, instance_id, json.dumps(result, ensure_ascii=False)))
            return result

    def resolve_reroll(self, request_id, accept):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT decided,data FROM rpg_witch_rest_reroll_receipts WHERE request_id=?',
                                  (request_id,)).fetchone()
            if not row:
                raise CharacterError('找不到這次重鑄。')
            decided, encoded = row
            result = json.loads(encoded)
            if decided:
                return result
            result['accepted'] = bool(accept)
            result['decided'] = True
            if accept:
                kind, grade, value = result['new']
                self.db.execute('''UPDATE rpg_instance_affixes SET affix_id=?,effect_key=?,rolled_value=?
                    WHERE instance_id=? AND affix_index=?''',
                    (f'witch:{kind}:{grade}', result['effect_key'], value,
                     result['instance_id'], result['affix_index']))
            self.db.execute('UPDATE rpg_witch_rest_reroll_receipts SET decided=1,data=? WHERE request_id=?',
                            (json.dumps(result, ensure_ascii=False), request_id))
            return result

    def upgrade(self, guild_id, user_id, instance_id):
        """Upgrade one T80 item in place, preserving its instance and rolls."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('''SELECT data FROM rpg_witch_rest_upgrade_receipts
                WHERE instance_id=? AND target_tier=90''', (instance_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            row = self.db.execute('''SELECT item_id FROM rpg_equipment_instances
                WHERE instance_id=? AND guild_id=? AND user_id=?''',
                (instance_id, guild_id, user_id)).fetchone()
            meta = self.db.execute('SELECT witch_id FROM rpg_witch_rest_equipment WHERE instance_id=?',
                                   (instance_id,)).fetchone()
            if not row or not meta or ':t80:' not in row[0]:
                raise CharacterError('只能昇階自己的 T80 魔女武器或套裝。')
            witch_id = meta[0]
            self._take_item(guild_id, user_id, f'witch_rest:{witch_id}:core', 12)
            self._take_item(guild_id, user_id, 'witch_rest:crystal', 1)
            self._take_gold(guild_id, user_id, 20_000, 'witch_rest_upgrade', str(instance_id))
            target = row[0].replace(':t80:', ':t90:')
            if target not in ITEMS:
                raise CharacterError('找不到對應的 T90 裝備定義。')
            self.db.execute('UPDATE rpg_equipment_instances SET item_id=? WHERE instance_id=?',
                            (target, instance_id))
            prefix = self.db.execute('''SELECT affix_id,effect_key FROM rpg_instance_affixes
                WHERE instance_id=? AND affix_index=0''', (instance_id,)).fetchone()
            if prefix:
                _, witch, tier, slug, slot = target.split(':')
                job = next(name for name, data in JOBS.items() if data[0] == slug)
                kind, grade = prefix[0].split(':')[1:]
                combat_index = ('vitality', 'assault', 'fortitude', 'prayer').index(kind)
                total = sum(piece[combat_index] for piece in BASE_COMBAT[90][job])
                value = round(total / .375 * (1 + int(grade)) / 100)
                self.db.execute('''UPDATE rpg_instance_affixes SET rolled_value=?
                    WHERE instance_id=? AND affix_index=0''', (value, instance_id))
            result = {'instance_id': instance_id, 'item_id': target, 'gold': 20_000}
            self.db.execute('INSERT INTO rpg_witch_rest_upgrade_receipts VALUES (?,90,?)',
                            (instance_id, json.dumps(result)))
            return result

    def direct_affix(self, request_id, guild_id, user_id, instance_id, affix_index, kind):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_processing_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            current = self._equipment_affix(guild_id, user_id, instance_id, affix_index)
            if kind not in {value for value, _ in affix_kinds(
                    current['job'], current['slot'], affix_index)}:
                raise CharacterError('這個部位不能使用指定詞條。')
            effect = self._affix_effect(affix_index, kind)
            if kind == current['kind']:
                raise CharacterError('新詞條必須與目前詞條不同。')
            memory = f'witch_rest:{current["witch_id"]}:directed_memory'
            self._take_item(guild_id, user_id, memory, 1)
            self._take_gold(guild_id, user_id, 5_000, 'witch_rest_direct_affix', request_id)
            value = self._affix_value(current['item_id'], current['job'], affix_index,
                                      kind, current['grade'])
            self.db.execute('''UPDATE rpg_instance_affixes SET affix_id=?,effect_key=?,rolled_value=?
                WHERE instance_id=? AND affix_index=?''',
                (f'witch:{kind}:{current["grade"]}', effect, value, instance_id, affix_index))
            result = dict(instance_id=instance_id, affix_index=affix_index, kind=kind,
                          grade=current['grade'], value=value, gold=5_000)
            self.db.execute('INSERT INTO rpg_witch_rest_processing_receipts VALUES (?,?,?,?,?)',
                            (request_id, guild_id, user_id, 'direct', json.dumps(result, ensure_ascii=False)))
            return result

    def improve_affix(self, request_id, guild_id, user_id, instance_id, affix_index):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_processing_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            current = self._equipment_affix(guild_id, user_id, instance_id, affix_index)
            if current['grade'] >= 4:
                raise CharacterError('這條詞條已經是 IV 級。')
            chance, gold = {1: (100, 1_000), 2: (80, 4_000), 3: (50, 10_000)}[current['grade']]
            self._take_gold(guild_id, user_id, gold, 'witch_rest_improve_affix', request_id)
            success = random.Random(request_id).random() * 100 < chance
            grade = current['grade'] + int(success)
            value = current['value']
            if success:
                self._take_item(guild_id, user_id, 'witch_rest:advanced_memory', 1)
                value = self._affix_value(current['item_id'], current['job'], affix_index,
                                          current['kind'], grade)
                self.db.execute('''UPDATE rpg_instance_affixes SET affix_id=?,rolled_value=?
                    WHERE instance_id=? AND affix_index=?''',
                    (f'witch:{current["kind"]}:{grade}', value, instance_id, affix_index))
            result = dict(instance_id=instance_id, affix_index=affix_index, success=success,
                          grade=grade, chance=chance, gold=gold, value=value)
            self.db.execute('INSERT INTO rpg_witch_rest_processing_receipts VALUES (?,?,?,?,?)',
                            (request_id, guild_id, user_id, 'improve', json.dumps(result)))
            return result

    def dismantle(self, request_id, guild_id, user_id, instance_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_processing_receipts WHERE request_id=?',
                                    (request_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            row = self.db.execute('''SELECT e.item_id,m.witch_id FROM rpg_equipment_instances e
                JOIN rpg_witch_rest_equipment m ON m.instance_id=e.instance_id
                WHERE e.instance_id=? AND e.guild_id=? AND e.user_id=?''',
                (instance_id, guild_id, user_id)).fetchone()
            if not row:
                raise CharacterError('找不到這件魔女裝備。')
            if self.db.execute('SELECT 1 FROM rpg_equipment WHERE instance_id=?', (instance_id,)).fetchone():
                raise CharacterError('請先卸下這件裝備。')
            item_id, witch_id = row
            grades = [int(row[0].rsplit(':', 1)[1]) for row in self.db.execute(
                'SELECT affix_id FROM rpg_instance_affixes WHERE instance_id=?', (instance_id,))]
            cores = 8 if ':t90:' in item_id else 4
            pages = sum(2 if grade == 4 else 1 if grade == 3 else 0 for grade in grades)
            add_owned_item(self.db, guild_id, user_id, f'witch_rest:{witch_id}:core', cores)
            if pages:
                add_owned_item(self.db, guild_id, user_id, 'witch_rest:memory_page', pages)
            self.db.execute('DELETE FROM rpg_instance_sockets WHERE instance_id=?', (instance_id,))
            self.db.execute('DELETE FROM rpg_instance_affixes WHERE instance_id=?', (instance_id,))
            self.db.execute('DELETE FROM rpg_witch_rest_equipment WHERE instance_id=?', (instance_id,))
            self.db.execute('DELETE FROM rpg_equipment_instances WHERE instance_id=?', (instance_id,))
            result = dict(instance_id=instance_id, cores=cores, pages=pages, witch_id=witch_id)
            self.db.execute('INSERT INTO rpg_witch_rest_processing_receipts VALUES (?,?,?,?,?)',
                            (request_id, guild_id, user_id, 'dismantle', json.dumps(result)))
            return result

    def progress(self, guild_id, user_id, witch_id):
        row = self.db.execute('''SELECT highest_enrage,total_wins,eligible_wins,dry_wins,luck_points,
            found_weapon,found_suit,found_accessory FROM rpg_witch_rest_progress
            WHERE guild_id=? AND user_id=? AND witch_id=?''', (guild_id, user_id, witch_id)).fetchone()
        values = row or (-1, 0, 0, 0, 0, 0, 0, 0)
        return dict(zip(('highest_enrage', 'total_wins', 'eligible_wins', 'dry_wins', 'luck_points',
                         'found_weapon', 'found_suit', 'found_accessory'), values))

    def settle(self, clear_id, guild_id, user_id, witch_id, job, enrage, *, seed):
        """Seal and deliver a victory exactly once; retries return the original roll."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.db.execute('SELECT data FROM rpg_witch_rest_rewards WHERE clear_id=? AND user_id=?',
                                    (clear_id, user_id)).fetchone()
            if saved:
                return json.loads(saved[0])
            progress = self.progress(guild_id, user_id, witch_id)
            missing = ('suit' if progress['found_weapon'] and not progress['found_suit'] else
                       'weapon' if progress['found_suit'] and not progress['found_weapon'] else None)
            result = roll_reward(random.Random(seed), witch_id, job, enrage,
                                 progress['dry_wins'], progress['luck_points'], missing)
            core_drop = enrage >= 1000 and random.Random(
                f'{seed}:alchemy-core').random() < .30
            if core_drop:
                result['items'][CORE_ITEM[3]] = result['items'].get(CORE_ITEM[3], 0) + 1
            result['alchemy_core'] = core_drop
            for item_id, quantity in result['items'].items():
                add_owned_item(self.db, guild_id, user_id, item_id, quantity)
            if result['gold']:
                self.db.execute('''INSERT INTO rpg_wallets VALUES (?,?,?) ON CONFLICT(guild_id,user_id)
                    DO UPDATE SET gold=gold+excluded.gold''', (guild_id, user_id, result['gold']))
                record_gold(self.db, guild_id, user_id, result['gold'], 'witch_rest_reward', clear_id)
            treasure = result['treasure']
            found_weapon = int(bool(treasure and treasure.endswith(':weapon')))
            found_suit = int(bool(treasure and treasure.endswith(':suit')))
            found_accessory = int(bool(treasure and treasure.endswith(':accessory')))
            if treasure:
                ids = add_owned_item(self.db, guild_id, user_id, treasure)
                if ids and result['affixes']:
                    affix_rows = []
                    effect_maps = ({'vitality': 'combat:0', 'assault': 'combat:1', 'fortitude': 'combat:2', 'prayer': 'combat:3'},
                                   {'precision': 'accuracy', 'haste': 'speed', 'critical': 'critical_points',
                                    'prowess': 'critical_damage_percent_add', 'stability': 'stability_lower',
                                    'drain': 'lifesteal', 'evasion': 'evasion', 'revival': 'healing_received_percent',
                                    'guard': 'direct_damage_reduction', 'corrosion': 'dot_damage_reduction',
                                    'unyielding': 'low_hp_damage_reduction'})
                    for index, (kind, grade) in enumerate(result['affixes']):
                        if index == 0:
                            combat_index = ('vitality', 'assault', 'fortitude', 'prayer').index(kind)
                            total = sum(piece[combat_index] for piece in BASE_COMBAT[80][job])
                            value = round(total / .35 * (1 + grade) / 100)
                        else:
                            values = {
                                'precision': (2, 4, 6, 8), 'haste': (1, 2, 3, 4),
                                'critical': (1, 2, 3, 4), 'prowess': (2, 4, 6, 8),
                                'stability': (2, 4, 6, 8), 'drain': (1, 2, 3, 4),
                                'evasion': (1, 2, 3, 4), 'revival': (3, 5, 7, 10),
                                'guard': (1, 2, 3, 4), 'corrosion': (5, 10, 15, 20),
                                'unyielding': (3, 5, 7, 10),
                            }
                            value = values[kind][grade - 1]
                        affix_rows.append((ids[0], index, f'witch:{kind}:{grade}', effect_maps[index][kind], value))
                    self.db.executemany('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)', affix_rows)
                    self.db.execute('INSERT INTO rpg_witch_rest_equipment VALUES (?,?,?,?,?,?)',
                                    (ids[0], witch_id, 0, 0, f'{clear_id}:{user_id}', 0))
                    result['instance_id'] = ids[0]
            self.db.execute('''INSERT INTO rpg_witch_rest_progress VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id,user_id,witch_id) DO UPDATE SET
                highest_enrage=max(highest_enrage,excluded.highest_enrage), total_wins=total_wins+1,
                eligible_wins=eligible_wins+excluded.eligible_wins, dry_wins=excluded.dry_wins,
                luck_points=excluded.luck_points, found_weapon=max(found_weapon,excluded.found_weapon),
                found_suit=max(found_suit,excluded.found_suit),
                found_accessory=max(found_accessory,excluded.found_accessory)''',
                (guild_id, user_id, witch_id, enrage, 1, int(enrage >= 100), result['dry_wins'],
                 result['luck_points'], found_weapon, found_suit, found_accessory))
            encoded = json.dumps(result, ensure_ascii=False)
            self.db.execute('INSERT INTO rpg_witch_rest_rewards(clear_id,user_id,guild_id,witch_id,enrage,data) VALUES (?,?,?,?,?,?)',
                            (clear_id, user_id, guild_id, witch_id, enrage, encoded))
            return result
