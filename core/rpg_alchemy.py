"""Alchemy dolls: bodies, cores, skill stones, powder, fuel and raid snapshots."""
from collections import Counter
import json
import math
import random
import time

from core.rpg import record_gold
from core.rpg_character import (CharacterError, GROWTH, ITEMS, Item, add_owned_item,
                                item_sell_price, item_sellable)
from core.rpg_farming import LOCATIONS, PLANTS
from core.rpg_fishing import SPOTS
from core.settings import RPGSettings


BODY_BUDGETS = {10: 140, 20: 260, 30: 360, 40: 460, 50: 580, 60: 680,
                70: 780, 80: 880, 90: 1000, 100: 1100, 110: 1200}
CRAFT_SECONDS = {10: 1800, 20: 3600, 30: 7200, 40: 14400, 50: 28800,
                 60: 43200, 70: 86400, 80: 129600, 90: 172800,
                 100: 216000, 110: 259200}
ACCURACY_BONUS = {10: 10, 20: 20, 30: 30, 40: 40, 50: 60, 60: 60,
                  70: 70, 80: 80, 90: 90, 100: 100, 110: 110}
STAT_NAMES = ('構造', '動力', '耐久', '精密', '靈質')
CORE_SLOTS = {
    (1, '戰鬥'): (3, 1), (1, '生活'): (1, 3),
    (2, '戰鬥'): (3, 2), (2, '生活'): (2, 3),
    (3, '戰鬥'): (4, 2), (3, '生活'): (2, 4),
}
RARITIES = {'普通': (70, .70, 1), '稀有': (24, .80, 3),
            '史詩': (5, .90, 10), '傳說': (1, 1.00, 30)}
COMBAT_RARITY_STATS = {
    '普通': {'multiplier': .90, 'cleanse': 1, 'uses': 1},
    '稀有': {'multiplier': 1.00, 'cleanse': 2, 'uses': 1},
    '史詩': {'multiplier': 1.10, 'cleanse': 2, 'uses': 2},
    '傳說': {'multiplier': 1.20, 'cleanse': 3, 'uses': 3},
}
RARITY_ORDER = tuple(RARITIES)
COMBAT_SKILLS = {
    'power_strike': '動力重擊', 'armor_break': '裂甲鑿擊', 'sweep': '廣域掃蕩',
    'overload': '超載猛擊', 'counter': '誘敵反擊', 'barrier': '防護力場',
    'rally': '緊急修復', 'repair': '修復射線', 'group_repair': '廣域修復',
    'amplify': '動力增幅', 'cleanse': '清除程序', 'interrupt': '妨害射擊',
}
COMBAT_SKILL_DETAILS = {
    'power_strike': '造成 160% 攻擊傷害',
    'armor_break': '造成 100% 攻擊傷害，並降低目標 80% 防禦',
    'sweep': '對全體敵人造成 120% 攻擊傷害',
    'overload': '對單一敵人造成 220% 攻擊傷害',
    'counter': '使一名隊友吸引單體攻擊，受到直接攻擊後以人偶攻擊反擊',
    'barrier': '依自身防禦提高全隊防禦',
    'rally': '依人偶構造修復主人',
    'repair': '依自身治療量恢復一名隊友',
    'group_repair': '恢復全體隊友各 65% 自身治療量',
    'amplify': '提高一名隊友 40% 攻擊',
    'cleanse': '移除一名隊友的負面狀態，數量依稀有度提升',
    'interrupt': '造成 120% 攻擊傷害，命中後打斷蓄力',
}
LIFE_SKILLS = {'fishing': '自律釣魚', 'farming': '自律農耕',
               'cooking': '自動備餐', 'raid_signup': '討伐響應'}
FARMING_WORK_THRESHOLDS = {'courtyard': 19, 'prison': 47, 'greenhouse': 94, 'ruins': 145}
LIFE_WORK_UNLOCKS = {
    'fishing': ((19, '30 分鐘'), (65, '2 小時'), (133, '8 小時')),
    'farming': tuple((threshold, LOCATIONS[location_id])
                     for location_id, threshold in FARMING_WORK_THRESHOLDS.items()),
    'raid_signup': ((19, '低階討伐'), (70, '中階討伐'), (122, '高階討伐')),
}
POWDER_COSTS = {'普通': 10, '稀有': 30, '史詩': 100}
CORE_ITEM = {level: f'alchemy:core:{level}' for level in (1, 2, 3)}
POWDER_ITEM = 'alchemy:powder'
GACHA_PRICE = 500
EPIC_PITY = 50
LEGEND_PITY = 100
CORE1_PROOF_COST = 15
BODY_ACCEL_GOLD_PER_HOUR = 500
FUEL_CAPACITY = 1000
BODY_MATERIAL_TIERS = tuple(range(10, 111, 10))


def body_material_id(tier, stat_index):
    return f'alchemy:body_material:{tier}:{stat_index}'


def parse_body_material(item_id):
    parts = item_id.split(':')
    if len(parts) != 4 or parts[:2] != ['alchemy', 'body_material']:
        return None
    try:
        tier, stat_index = int(parts[2]), int(parts[3])
    except ValueError:
        return None
    if tier not in BODY_MATERIAL_TIERS or stat_index not in range(len(STAT_NAMES)):
        return None
    return tier, tuple(1.0 if index == stat_index else 0.0
                       for index in range(len(STAT_NAMES)))
LOW_FUEL_OPERATION_THRESHOLD = 5


def raid_signup_pool(raid):
    """Apply mid-tier signup filters to paint-set Noah summons."""
    return 'mid' if raid.get('pool') == 'special' else raid.get('pool')


def stone_id(domain, skill, rarity):
    return f'alchemy:stone:{domain}:{skill}:{RARITY_ORDER.index(rarity)}'


def parse_stone(item_id):
    parts = item_id.split(':')
    if len(parts) != 5 or parts[:2] != ['alchemy', 'stone']:
        return None
    domain, skill = parts[2], parts[3]
    try:
        rarity = RARITY_ORDER[int(parts[4])]
    except (ValueError, IndexError):
        return None
    pool = COMBAT_SKILLS if domain == 'combat' else LIFE_SKILLS if domain == 'life' else {}
    return (domain, skill, rarity) if skill in pool else None


def life_skill_unlocks(key, work):
    """Return the automation capabilities unlocked by an effective work score."""
    if key == 'cooking':
        return (f'美味度 {work} 以下的保存配方',)
    return tuple(label for threshold, label in LIFE_WORK_UNLOCKS.get(key, ())
                 if work >= threshold)


def life_work(body, key, rarity):
    """Calculate a life skill's effective work for an arbitrary body and stone."""
    if not body or key not in LIFE_SKILLS or rarity not in RARITIES:
        return None
    primary, secondary = {'fishing': (3, 2), 'farming': (1, 0),
                          'cooking': (3, 4), 'raid_signup': (3, 0)}[key]
    stats = body['stats']
    multiplier = RARITIES[rarity][1]
    return math.floor((stats[primary] * 2 + stats[secondary]) / 3 * multiplier)


def _register_items():
    ITEMS[POWDER_ITEM] = Item('鍊金粉塵', '', '', 0, (0,) * 5, category='製作材料',
                              description='分解技能石取得；可兌換指定的普通、稀有或史詩技能石。',
                              transferable=False)
    for level in (1, 2, 3):
        ITEMS[CORE_ITEM[level]] = Item(
            f'Lv.{level} 思考核心胚', '', '', 0, (0,) * 5, category='製作材料',
            description='未定向時可交易；定向為戰鬥或生活面向後綁定。')
    for tier in BODY_MATERIAL_TIERS:
        for stat_index, name in enumerate(STAT_NAMES):
            ITEMS[body_material_id(tier, stat_index)] = Item(
                f'T{tier} {name}素體素材', '', '', 0, (0,) * 5, category='製作材料',
                description=f'人偶遠征取得；製作素體時只提高{name}傾向。',
                transferable=True)
    for domain, pool in (('combat', COMBAT_SKILLS), ('life', LIFE_SKILLS)):
        for key, name in pool.items():
            for rarity, (_, multiplier, _) in RARITIES.items():
                detail = (COMBAT_SKILL_DETAILS[key] if domain == 'combat'
                          else '最終工作力由素體能力與稀有度共同決定')
                if domain == 'combat':
                    combat_rarity = COMBAT_RARITY_STATS[rarity]
                    rarity_effect = (f'每次可移除 {combat_rarity["cleanse"]} 個負面狀態'
                                     if key == 'cleanse' else
                                     f'效果倍率 {combat_rarity["multiplier"]:.0%}')
                    rarity_effect += f'；每場可用 {combat_rarity["uses"]} 次'
                else:
                    rarity_effect = f'效果倍率 {multiplier:.0%}'
                ITEMS[stone_id(domain, key, rarity)] = Item(
                    f'{rarity}・{name}技能石', '', '', 0, (0,) * 5, category='製作材料',
                    description=f'{detail}；{rarity_effect}。')


_register_items()


def _floor_tier(level):
    return max(10, min(110, level // 10 * 10))


def body_acceleration_cost(craft, now=None):
    if not craft:
        return 0
    now = time.time() if now is None else now
    return math.ceil(max(0, craft['ready_at'] - now) * BODY_ACCEL_GOLD_PER_HOUR / 3600)


def fuel_value(item):
    return max(1, item_sell_price(item))


def fuel_discount(durability):
    """Return the durability fuel discount, with smooth diminishing returns."""
    durability = max(0, int(durability))
    return min(70, round(100 * durability / (durability + 100)))


def operation_fuel_cost(body, operations=1):
    if not body or type(operations) is not int or operations < 1:
        raise ValueError('body and a positive operation count are required')
    discount = fuel_discount(body['stats'][2])
    return math.ceil(100 * operations * (100 - discount) / 100)


def material_profile(item_id):
    """Return (tier, five tendency weights), or None when the item is ineligible."""
    expedition_material = parse_body_material(item_id)
    if expedition_material:
        return expedition_material
    if item_id.startswith('alchemy:') or item_id not in ITEMS:
        return None
    item = ITEMS[item_id]
    if item.category in ('裝備', '釣竿') or not item_sellable(item):
        return None
    zero = [0.0] * 5
    if item_id.startswith('fishing:'):
        parts = item_id.split(':')
        if len(parts) < 3 or parts[1] == 'rod':
            return None
        spot_id = parts[2] if parts[1] == 'boss' else parts[1]
        spot = SPOTS.get(spot_id)
        if not spot:
            return None
        tier = _floor_tier(spot.level)
        kind = 'boss' if parts[1] == 'boss' else parts[2]
        pair = ((2, 4) if kind == 'weed' else (0, 2) if kind == 'rod'
                else (3, 4) if kind == 'line' else (0, 1) if kind == 'boss'
                else (1, 3))
    elif item_id.startswith('farming:'):
        plant = next((plant for plant in PLANTS.values() if plant.item_id == item_id), None)
        if not plant:
            return None
        tier, pair = _floor_tier(plant.level), (0, 1)
    elif item_id.startswith('cooking:meat:'):
        tier, pair = {'low': 10, 'mid': 30, 'high': 50}[item_id.rsplit(':', 1)[1]], (0, 1)
    elif item_id.startswith('cooking:seasoning:'):
        tier, pair = {'low': 10, 'mid': 30, 'high': 50}[item_id.rsplit(':', 1)[1]], (3, 4)
    elif item.category in ('料理素材', '製作材料', '換金道具'):
        tier, pair = 10, (2, 4)
    else:
        return None
    for index in pair:
        zero[index] += .5
    return tier, tuple(zero)


def _base_stats(job, level, settings):
    from core.rpg_character import stage_for
    stage = stage_for(level, settings)
    return tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                 + stage * weight * 2 for weight in GROWTH[job])


def stat_caps(level, settings=None):
    settings = settings or RPGSettings()
    jobs = tuple(job for job in GROWTH if job != '民兵')
    return tuple(max(_base_stats(job, level, settings)[index] for job in jobs)
                 for index in range(5))


def roll_body(tier, tendency, seed, settings=None):
    rng = random.Random(seed)
    weights = [.1 + .5 * value for value in tendency]
    caps = stat_caps(tier, settings)
    stats = [0] * 5
    for _ in range(BODY_BUDGETS[tier]):
        available = [index for index in range(5) if stats[index] < caps[index]]
        roll = rng.random() * sum(weights[index] for index in available)
        for index in available:
            roll -= weights[index]
            if roll < 0:
                stats[index] += 1
                break
    return tuple(stats)


class AlchemyDolls:
    def __init__(self, store, settings=None, rng=None):
        self.store, self.db = store, store.db
        self.settings = settings or RPGSettings()
        self.rng = rng or random.Random()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_alchemy_dolls (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '煉金人偶',
                active_body TEXT, candidate_body TEXT, crafting TEXT, fuel INTEGER NOT NULL DEFAULT 0,
                registered INTEGER NOT NULL DEFAULT 0, last_selected REAL NOT NULL DEFAULT 0,
                life_config TEXT NOT NULL DEFAULT '{}',
                fuel_low_notified INTEGER NOT NULL DEFAULT 0,
                fuel_low_pending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id))''')
            doll_columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_alchemy_dolls)')}
            if 'fuel_low_notified' not in doll_columns:
                self.db.execute('ALTER TABLE rpg_alchemy_dolls ADD COLUMN '
                                'fuel_low_notified INTEGER NOT NULL DEFAULT 0')
            if 'fuel_low_pending' not in doll_columns:
                self.db.execute('ALTER TABLE rpg_alchemy_dolls ADD COLUMN '
                                'fuel_low_pending INTEGER NOT NULL DEFAULT 0')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_alchemy_cores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                level INTEGER NOT NULL, orientation TEXT NOT NULL, skills TEXT NOT NULL,
                equipped INTEGER NOT NULL DEFAULT 0)''')
            self.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_equipped_alchemy_core
                ON rpg_alchemy_cores(guild_id,user_id) WHERE equipped=1''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_alchemy_pity (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                epic_misses INTEGER NOT NULL DEFAULT 0, legend_misses INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_alchemy_operations (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL, fuel INTEGER NOT NULL,
                result TEXT, created_at REAL NOT NULL,
                PRIMARY KEY(guild_id,user_id,kind,source))''')

    def _ensure(self, guild, user):
        self.db.execute('INSERT OR IGNORE INTO rpg_alchemy_dolls(guild_id,user_id) VALUES (?,?)',
                        (guild, user))

    def state(self, guild, user):
        if not self.db.in_transaction:
            with self.db:
                self._ensure(guild, user)
        else:
            self._ensure(guild, user)
        row = self.db.execute('''SELECT name,active_body,candidate_body,crafting,fuel,life_config
            FROM rpg_alchemy_dolls WHERE guild_id=? AND user_id=?''',
                              (guild, user)).fetchone()
        core = self.db.execute('''SELECT id,level,orientation,skills FROM rpg_alchemy_cores
            WHERE guild_id=? AND user_id=? AND equipped=1''', (guild, user)).fetchone()
        return dict(name=row[0], active_body=json.loads(row[1]) if row[1] else None,
                    candidate_body=json.loads(row[2]) if row[2] else None,
                    crafting=json.loads(row[3]) if row[3] else None, fuel=row[4],
                    config=json.loads(row[5]),
                    core=(dict(id=core[0], level=core[1], orientation=core[2],
                               skills=json.loads(core[3])) if core else None))

    def start_body(self, guild, user, materials, now=None):
        now = time.time() if now is None else now
        counts = Counter(materials)
        if len(materials) != 10 or not 1 <= len(counts) <= 5:
            raise CharacterError('素體固定需要十份素材，且最多使用五種不同素材。')
        profiles = {key: material_profile(key) for key in counts}
        if any(profile is None for profile in profiles.values()):
            raise CharacterError('素材包含不能用於素體的物品。')
        tiers = [profiles[key][0] for key in materials]
        tier = _floor_tier(sum(tiers) // 10)
        tendency = [sum(profiles[key][1][index] for key in materials) / 10 for index in range(5)]
        payload = dict(materials=dict(counts), tier=tier, tendency=tendency,
                       seed=self.rng.randrange(2**63), started_at=now,
                       ready_at=now + CRAFT_SECONDS[tier])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('''SELECT candidate_body,crafting FROM rpg_alchemy_dolls
                WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
            if row[0]:
                raise CharacterError('請先安裝或拆除候選素體。')
            if row[1]:
                raise CharacterError('已有素體正在製作。')
            for key, quantity in counts.items():
                paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                    WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=?''',
                                       (quantity, guild, user, key, quantity))
                if not paid.rowcount:
                    raise CharacterError(f'{ITEMS[key].name}數量不足。')
                self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                                'AND item_id=? AND quantity=0', (guild, user, key))
            self.db.execute('UPDATE rpg_alchemy_dolls SET crafting=? WHERE guild_id=? AND user_id=?',
                            (json.dumps(payload, ensure_ascii=False), guild, user))
        return payload

    def finish_body(self, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT crafting,candidate_body FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            if row[1]:
                return json.loads(row[1])
            if not row[0]:
                raise CharacterError('目前沒有正在製作的素體。')
            craft = json.loads(row[0])
            if now < craft['ready_at']:
                raise CharacterError('素體尚未完成。')
            stats = roll_body(craft['tier'], craft['tendency'], craft['seed'], self.settings)
            body = dict(tier=craft['tier'], stats=stats, materials=craft['materials'],
                        tendency=craft['tendency'], seed=craft['seed'],
                        started_at=craft['started_at'], completed_at=craft['ready_at'])
            self.db.execute('''UPDATE rpg_alchemy_dolls SET candidate_body=?,crafting=NULL
                WHERE guild_id=? AND user_id=?''',
                            (json.dumps(body, ensure_ascii=False), guild, user))
        return body

    def accelerate_body(self, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT crafting FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            if not row[0]:
                raise CharacterError('目前沒有正在製作的素體。')
            craft = json.loads(row[0])
            cost = body_acceleration_cost(craft, now)
            if cost:
                paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                    WHERE guild_id=? AND user_id=? AND gold>=?''', (cost, guild, user, cost))
                if not paid.rowcount:
                    raise CharacterError(f'金幣不足，需要 {cost:,} 金幣。')
                record_gold(self.db, guild, user, -cost, 'alchemy_body_acceleration')
                craft['ready_at'] = now
                self.db.execute('UPDATE rpg_alchemy_dolls SET crafting=? '
                                'WHERE guild_id=? AND user_id=?',
                                (json.dumps(craft, ensure_ascii=False), guild, user))
        return cost

    def install_candidate(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT candidate_body FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            if not row[0]:
                raise CharacterError('目前沒有候選素體。')
            self.db.execute('''UPDATE rpg_alchemy_dolls SET active_body=candidate_body,
                candidate_body=NULL WHERE guild_id=? AND user_id=?''', (guild, user))
        return json.loads(row[0])

    def discard_candidate(self, guild, user):
        with self.db:
            changed = self.db.execute('''UPDATE rpg_alchemy_dolls SET candidate_body=NULL
                WHERE guild_id=? AND user_id=? AND candidate_body IS NOT NULL''', (guild, user))
            if not changed.rowcount:
                raise CharacterError('目前沒有候選素體。')

    def grant_core(self, guild, user, level):
        if level not in CORE_ITEM:
            raise CharacterError('無效的思考核心等級。')
        with self.db:
            add_owned_item(self.db, guild, user, CORE_ITEM[level])

    def buy_core1(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                WHERE guild_id=? AND user_id=? AND item_id='proof:raid' AND quantity>=?''',
                                   (CORE1_PROOF_COST, guild, user, CORE1_PROOF_COST))
            if not paid.rowcount:
                raise CharacterError(f'討伐之證不足，需要 {CORE1_PROOF_COST} 個。')
            self.db.execute("DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? "
                            "AND item_id='proof:raid' AND quantity=0", (guild, user))
            add_owned_item(self.db, guild, user, CORE_ITEM[1])

    def orient_core(self, guild, user, level, orientation):
        if (level, orientation) not in CORE_SLOTS:
            raise CharacterError('無效的核心等級或面向。')
        combat, life = CORE_SLOTS[level, orientation]
        skills = {'combat': [None] * combat, 'life': [None] * life}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=1''',
                                   (guild, user, CORE_ITEM[level]))
            if not paid.rowcount:
                raise CharacterError('背包中沒有這個思考核心胚。')
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, CORE_ITEM[level]))
            equipped = not self.db.execute('''SELECT 1 FROM rpg_alchemy_cores
                WHERE guild_id=? AND user_id=? AND equipped=1''', (guild, user)).fetchone()
            cursor = self.db.execute('''INSERT INTO rpg_alchemy_cores
                (guild_id,user_id,level,orientation,skills,equipped) VALUES (?,?,?,?,?,?)''',
                (guild, user, level, orientation, json.dumps(skills), int(equipped)))
        return cursor.lastrowid

    def cores(self, guild, user):
        return [dict(id=row[0], level=row[1], orientation=row[2], skills=json.loads(row[3]),
                     equipped=bool(row[4])) for row in self.db.execute('''SELECT id,level,orientation,
            skills,equipped FROM rpg_alchemy_cores WHERE guild_id=? AND user_id=? ORDER BY id''',
                                                                        (guild, user))]

    def equip_core(self, guild, user, core_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT 1 FROM rpg_alchemy_cores
                WHERE id=? AND guild_id=? AND user_id=?''', (core_id, guild, user)).fetchone()
            if not row:
                raise CharacterError('找不到這枚思考核心。')
            self.db.execute('UPDATE rpg_alchemy_cores SET equipped=0 WHERE guild_id=? AND user_id=?',
                            (guild, user))
            self.db.execute('UPDATE rpg_alchemy_cores SET equipped=1 WHERE id=?', (core_id,))

    def dismantle_core(self, guild, user, core_id):
        """Destroy one oriented core and turn all of its engraved stones into powder."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT skills,equipped FROM rpg_alchemy_cores
                WHERE id=? AND guild_id=? AND user_id=?''', (core_id, guild, user)).fetchone()
            if not row:
                raise CharacterError('找不到這枚思考核心。')
            skills = json.loads(row[0])
            engraved = [stone for domain in skills.values() for stone in domain if stone]
            powder = sum(RARITIES[stone['rarity']][2] for stone in engraved)
            self.db.execute('DELETE FROM rpg_alchemy_cores WHERE id=?', (core_id,))
            if powder:
                add_owned_item(self.db, guild, user, POWDER_ITEM, powder)
        return dict(stones=len(engraved), powder=powder, equipped=bool(row[1]))

    def engrave(self, guild, user, core_id, domain, slot, item_id):
        stone = parse_stone(item_id)
        if not stone or stone[0] != domain or type(slot) is not int:
            raise CharacterError('技能石與刻印迴路不相符。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT skills FROM rpg_alchemy_cores
                WHERE id=? AND guild_id=? AND user_id=?''', (core_id, guild, user)).fetchone()
            if not row:
                raise CharacterError('找不到這枚思考核心。')
            skills = json.loads(row[0])
            if not 1 <= slot <= len(skills[domain]):
                raise CharacterError('無效的刻印格。')
            current = skills[domain][slot - 1]
            if current and current['key'] == stone[1] and current['rarity'] == stone[2]:
                raise CharacterError('這個刻印格已經是完全相同的技能石。')
            if any(index != slot - 1 and saved and saved['key'] == stone[1]
                   for index, saved in enumerate(skills[domain])):
                raise CharacterError('同一迴路不能重複刻入相同技能。')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=1''',
                                   (guild, user, item_id))
            if not paid.rowcount:
                raise CharacterError('背包中沒有這顆技能石。')
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, item_id))
            skills[domain][slot - 1] = {'key': stone[1], 'rarity': stone[2]}
            self.db.execute('UPDATE rpg_alchemy_cores SET skills=? WHERE id=?',
                            (json.dumps(skills, ensure_ascii=False), core_id))
        return skills

    def _draw_rarity(self, epic_misses, legend_misses):
        if legend_misses >= LEGEND_PITY - 1:
            return '傳說'
        if epic_misses >= EPIC_PITY - 1:
            return '傳說' if self.rng.random() < 1 / 6 else '史詩'
        roll = self.rng.random() * 100
        total = 0
        for rarity, (chance, _, _) in RARITIES.items():
            total += chance
            if roll < total:
                return rarity
        return '傳說'

    def pity(self, guild, user):
        row = self.db.execute('''SELECT epic_misses,legend_misses FROM rpg_alchemy_pity
            WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone() or (0, 0)
        return dict(epic_misses=row[0], legend_misses=row[1],
                    epic_remaining=max(1, EPIC_PITY - row[0]),
                    legend_remaining=max(1, LEGEND_PITY - row[1]))

    def draw(self, guild, user, domain, count=1):
        pool = COMBAT_SKILLS if domain == 'combat' else LIFE_SKILLS if domain == 'life' else None
        if pool is None or count not in (1, 10):
            raise CharacterError('只能選擇戰鬥或生活卡池，並進行單抽或十連。')
        cost = GACHA_PRICE * count
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''', (cost, guild, user, cost))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，需要 {cost:,} 金幣。')
            record_gold(self.db, guild, user, -cost, 'alchemy_gacha')
            row = self.db.execute('''SELECT epic_misses,legend_misses FROM rpg_alchemy_pity
                WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone() or (0, 0)
            epic_misses, legend_misses = row
            results = []
            for index in range(count):
                rarity = self._draw_rarity(epic_misses, legend_misses)
                if count == 10 and index == 9 and all(saved[1] == '普通' for saved in results):
                    rarity = self.rng.choices(('稀有', '史詩', '傳說'), weights=(24, 5, 1))[0]
                epic_misses = 0 if RARITY_ORDER.index(rarity) >= 2 else epic_misses + 1
                legend_misses = 0 if rarity == '傳說' else legend_misses + 1
                results.append([self.rng.choice(tuple(pool)), rarity])
            self.db.execute('''INSERT INTO rpg_alchemy_pity VALUES (?,?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET epic_misses=excluded.epic_misses,
                legend_misses=excluded.legend_misses''', (guild, user, epic_misses, legend_misses))
            for skill, rarity in results:
                add_owned_item(self.db, guild, user, stone_id(domain, skill, rarity))
        return [dict(domain=domain, skill=skill, rarity=rarity,
                     item_id=stone_id(domain, skill, rarity)) for skill, rarity in results]

    def decompose(self, guild, user, item_id, quantity=1):
        stone = parse_stone(item_id)
        if not stone or quantity < 1:
            raise CharacterError('只能分解未刻入的技能石。')
        powder = RARITIES[stone[2]][2] * quantity
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=?''',
                                   (quantity, guild, user, item_id, quantity))
            if not paid.rowcount:
                raise CharacterError('技能石數量不足。')
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, item_id))
            add_owned_item(self.db, guild, user, POWDER_ITEM, powder)
        return powder

    def decompose_many(self, guild, user, item_ids):
        keys = tuple(dict.fromkeys(item_ids))
        if not keys or any(not parse_stone(key) for key in keys):
            raise CharacterError('請選擇要分解的技能石。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = dict(self.db.execute(
                f'''SELECT item_id,quantity FROM rpg_inventory
                    WHERE guild_id=? AND user_id=? AND item_id IN ({','.join('?' * len(keys))})''',
                (guild, user, *keys)).fetchall())
            if not rows:
                raise CharacterError('已沒有可分解的技能石。')
            powder = sum(RARITIES[parse_stone(key)[2]][2] * quantity
                         for key, quantity in rows.items())
            quantity = sum(rows.values())
            self.db.executemany('''DELETE FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id=?''',
                                ((guild, user, key) for key in rows))
            add_owned_item(self.db, guild, user, POWDER_ITEM, powder)
        return quantity, powder

    def exchange_stone(self, guild, user, domain, skill, rarity):
        pool = COMBAT_SKILLS if domain == 'combat' else LIFE_SKILLS if domain == 'life' else {}
        if skill not in pool or rarity not in POWDER_COSTS:
            raise CharacterError('粉塵只能兌換普通、稀有或史詩技能石。')
        cost = POWDER_COSTS[rarity]
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=?''',
                                   (cost, guild, user, POWDER_ITEM, cost))
            if not paid.rowcount:
                raise CharacterError(f'鍊金粉塵不足，需要 {cost}。')
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, POWDER_ITEM))
            item_id = stone_id(domain, skill, rarity)
            add_owned_item(self.db, guild, user, item_id)
        return item_id

    def convert_fuel(self, guild, user, item_id, quantity):
        item = ITEMS.get(item_id)
        if (not item or type(quantity) is not int or quantity < 1 or item_id.startswith('alchemy:')
                or item.category in ('裝備', '釣竿')
                or not item_sellable(item)):
            raise CharacterError('這項物品不能轉換為鍊金燃料。')
        fuel = fuel_value(item) * quantity
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT fuel,active_body FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            current, body_raw = row
            if current >= FUEL_CAPACITY:
                raise CharacterError(f'燃料已達上限 {FUEL_CAPACITY:,}。')
            gained = min(fuel, FUEL_CAPACITY - current)
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=?''',
                                   (quantity, guild, user, item_id, quantity))
            if not paid.rowcount:
                raise CharacterError('素材數量不足。')
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, item_id))
            updated = current + gained
            next_cost = operation_fuel_cost(json.loads(body_raw)) if body_raw else 100
            low_fuel_threshold = next_cost * LOW_FUEL_OPERATION_THRESHOLD
            self.db.execute('''UPDATE rpg_alchemy_dolls SET fuel=?,
                fuel_low_notified=CASE WHEN ?>= ? THEN 0 ELSE fuel_low_notified END,
                fuel_low_pending=CASE WHEN ?>= ? THEN 0 ELSE fuel_low_pending END
                WHERE guild_id=? AND user_id=?''',
                (updated, updated, low_fuel_threshold,
                 updated, low_fuel_threshold, guild, user))
        return gained

    def life_skill(self, guild, user, key):
        """Return the equipped life skill and its effective work score."""
        state = self.state(guild, user)
        if not state['active_body'] or not state['core']:
            return None
        saved = next((entry for entry in state['core']['skills']['life']
                      if entry and entry['key'] == key), None)
        if not saved:
            return None
        work = life_work(state['active_body'], key, saved['rarity'])
        return dict(key=key, rarity=saved['rarity'], work=work)

    def configure_life(self, guild, user, key, *, enabled=None, preset_slot=None,
                       pools=None, qualities=None, scheduled=None, bounty=None,
                       require_no_effect=None):
        if key not in LIFE_SKILLS:
            raise CharacterError('無效的生活技能。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT life_config FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            config = json.loads(row[0])
            saved = config.setdefault(key, {'enabled': False})
            if enabled is not None:
                saved['enabled'] = bool(enabled)
            if preset_slot is not None:
                saved['preset_slot'] = int(preset_slot)
            if pools is not None:
                saved['pools'] = list(pools)
            if qualities is not None:
                saved['qualities'] = list(qualities)
            if scheduled is not None:
                saved['scheduled'] = bool(scheduled)
            if bounty is not None:
                saved['bounty'] = bool(bounty)
            if require_no_effect is not None:
                saved['require_no_effect'] = bool(require_no_effect)
            self.db.execute('UPDATE rpg_alchemy_dolls SET life_config=? '
                            'WHERE guild_id=? AND user_id=?',
                            (json.dumps(config, ensure_ascii=False), guild, user))
        return saved

    def set_combat_support(self, guild, user, enabled):
        if type(enabled) is not bool:
            raise CharacterError('無效的戰鬥支援設定。')
        if enabled:
            state = self.state(guild, user)
            if not state['active_body'] or not state['core']:
                raise CharacterError('必須先安裝素體與思考核心。')
            if not any(state['core']['skills']['combat']):
                raise CharacterError('目前核心沒有刻入戰鬥技能石。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            row = self.db.execute('SELECT life_config FROM rpg_alchemy_dolls '
                                  'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
            config = json.loads(row[0])
            config.setdefault('combat_support', {})['enabled'] = enabled
            self.db.execute('UPDATE rpg_alchemy_dolls SET life_config=? '
                            'WHERE guild_id=? AND user_id=?',
                            (json.dumps(config, ensure_ascii=False), guild, user))
        return enabled

    def _fuel_cost(self, body, operations):
        return operation_fuel_cost(body, operations)

    def _reserve_operation(self, guild, user, kind, source, operations, now):
        """Idempotently reserve fuel before an automated state change."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure(guild, user)
            existing = self.db.execute('''SELECT status,fuel,result FROM rpg_alchemy_operations
                WHERE guild_id=? AND user_id=? AND kind=? AND source=?''',
                                       (guild, user, kind, source)).fetchone()
            if existing:
                return dict(status=existing[0], fuel=existing[1],
                            result=json.loads(existing[2]) if existing[2] else None)
            from core.rpg_expeditions import require_doll_available
            require_doll_available(self.db, guild, user, now)
            body_row = self.db.execute('''SELECT active_body,fuel,fuel_low_notified
                FROM rpg_alchemy_dolls WHERE guild_id=? AND user_id=?''',
                                       (guild, user)).fetchone()
            if not body_row[0]:
                raise CharacterError('人偶尚未安裝素體。')
            cost = self._fuel_cost(json.loads(body_row[0]), operations)
            if body_row[1] < cost:
                raise CharacterError(f'鍊金燃料不足，需要 {cost}。')
            remaining = body_row[1] - cost
            alert = remaining < cost * LOW_FUEL_OPERATION_THRESHOLD and not body_row[2]
            self.db.execute('''UPDATE rpg_alchemy_dolls SET fuel=?,
                fuel_low_notified=CASE WHEN ? THEN 1 ELSE fuel_low_notified END,
                fuel_low_pending=CASE WHEN ? THEN 1 ELSE fuel_low_pending END
                WHERE guild_id=? AND user_id=?''',
                (remaining, int(alert), int(alert), guild, user))
            self.db.execute('INSERT INTO rpg_alchemy_operations VALUES (?,?,?,?,?,?,?,?)',
                            (guild, user, kind, source, 'reserved', cost, None, now))
        return dict(status='reserved', fuel=cost, result=None,
                    remaining_fuel=remaining, low_fuel_alert=alert)

    def _finish_operation(self, guild, user, kind, source, result):
        with self.db:
            self.db.execute('''UPDATE rpg_alchemy_operations SET status='completed',result=?
                WHERE guild_id=? AND user_id=? AND kind=? AND source=?''',
                (json.dumps(result, ensure_ascii=False, default=str), guild, user, kind, source))

    def _cancel_operation(self, guild, user, kind, source):
        with self.db:
            row = self.db.execute('''SELECT fuel,status FROM rpg_alchemy_operations
                WHERE guild_id=? AND user_id=? AND kind=? AND source=?''',
                                  (guild, user, kind, source)).fetchone()
            if row and row[1] == 'reserved':
                self.db.execute('UPDATE rpg_alchemy_dolls SET fuel=fuel+? '
                                'WHERE guild_id=? AND user_id=?', (row[0], guild, user))
                self.db.execute("UPDATE rpg_alchemy_operations SET status='failed' "
                                'WHERE guild_id=? AND user_id=? AND kind=? AND source=?',
                                (guild, user, kind, source))
                doll = self.db.execute('SELECT active_body,fuel FROM rpg_alchemy_dolls '
                                       'WHERE guild_id=? AND user_id=?',
                                       (guild, user)).fetchone()
                next_cost = operation_fuel_cost(json.loads(doll[0])) if doll[0] else 100
                if doll[1] >= next_cost * LOW_FUEL_OPERATION_THRESHOLD:
                    self.db.execute('''UPDATE rpg_alchemy_dolls SET
                        fuel_low_notified=0,fuel_low_pending=0
                        WHERE guild_id=? AND user_id=?''', (guild, user))

    def fuel_alerts_due(self):
        result = []
        rows = self.db.execute('''SELECT guild_id,user_id,fuel,active_body
            FROM rpg_alchemy_dolls WHERE fuel_low_pending=1
            ORDER BY guild_id,user_id''').fetchall()
        for guild, user, fuel, body_raw in rows:
            cost = operation_fuel_cost(json.loads(body_raw)) if body_raw else 100
            result.append((guild, user, fuel, cost))
        return result

    def reserve_fuel_alert(self, guild, user):
        with self.db:
            changed = self.db.execute('''UPDATE rpg_alchemy_dolls SET fuel_low_pending=0
                WHERE guild_id=? AND user_id=? AND fuel_low_pending=1''', (guild, user))
        return bool(changed.rowcount)

    def auto_signup_candidates(self, raid):
        pool = raid_signup_pool(raid)
        if raid.get('source') not in (None, 'bounty') or pool not in ('regular', 'mid', 'high'):
            return []
        threshold = {'regular': 19, 'mid': 70, 'high': 122}[pool]
        result = []
        rows = self.db.execute('SELECT user_id,life_config FROM rpg_alchemy_dolls '
                               'WHERE guild_id=?', (raid['guild_id'],)).fetchall()
        for user, raw_config in rows:
            from core.rpg_expeditions import is_doll_expedition_active
            if is_doll_expedition_active(self.db, raid['guild_id'], user):
                continue
            config = json.loads(raw_config).get('raid_signup', {})
            skill = self.life_skill(raid['guild_id'], user, 'raid_signup')
            if not config.get('enabled') or not skill or skill['work'] < threshold:
                continue
            if pool not in config.get('pools', ('regular', 'mid', 'high')):
                continue
            if raid['monster'].get('quality', '普通') not in config.get(
                    'qualities', ('普通', '精英', '首領', '傳說')):
                continue
            source_key = 'bounty' if raid.get('source') == 'bounty' else 'scheduled'
            if not config.get(source_key, True):
                continue
            result.append(user)
        return result

    def auto_cooking_candidates(self, raid, provisions, now=None):
        now = time.time() if now is None else now
        pool = raid.get('pool')
        if raid.get('source') not in (None, 'bounty') or pool not in ('regular', 'mid', 'high'):
            return []
        result = []
        for user, raw_config in self.db.execute('SELECT user_id,life_config FROM rpg_alchemy_dolls '
                                                'WHERE guild_id=?', (raid['guild_id'],)):
            from core.rpg_expeditions import is_doll_expedition_active
            if is_doll_expedition_active(self.db, raid['guild_id'], user, now):
                continue
            config = json.loads(raw_config).get('cooking', {})
            skill = self.life_skill(raid['guild_id'], user, 'cooking')
            if not config.get('enabled') or not skill or not config.get('preset_slot'):
                continue
            if pool not in config.get('pools', ('regular', 'mid', 'high')):
                continue
            if raid['monster'].get('quality', '普通') not in config.get(
                    'qualities', ('普通', '精英', '首領', '傳說')):
                continue
            source_key = 'bounty' if raid.get('source') == 'bounty' else 'scheduled'
            if not config.get(source_key, True):
                continue
            preset = provisions.preset(raid['guild_id'], user, config['preset_slot'])
            if not preset['ingredients']:
                continue
            preview = provisions.preview(preset['ingredients'], raid['guild_id'], user)
            if skill['work'] < preview['score']:
                continue
            if config.get('require_no_effect', True) and provisions._active_meal(
                    raid['guild_id'], user, now):
                continue
            if provisions._host_has_open_table(raid['guild_id'], user, now):
                continue
            result.append((user, preset['ingredients']))
        return result

    def reserve_cooking(self, guild, user, raid_id, now=None):
        now = time.time() if now is None else now
        return self._reserve_operation(guild, user, 'cooking', raid_id, 1, now)

    def finish_cooking(self, guild, user, raid_id, fuel, meal_id):
        self._finish_operation(guild, user, 'cooking', raid_id,
                               {'raid_id': raid_id, 'meal_id': meal_id, 'fuel': fuel})

    def cancel_cooking(self, guild, user, raid_id):
        self._cancel_operation(guild, user, 'cooking', raid_id)

    def reserve_signup(self, guild, user, raid_id, now=None):
        now = time.time() if now is None else now
        return self._reserve_operation(guild, user, 'raid_signup', raid_id, 1, now)

    def finish_signup(self, guild, user, raid_id, fuel):
        self._finish_operation(guild, user, 'raid_signup', raid_id,
                               {'raid_id': raid_id, 'fuel': fuel})

    def cancel_signup(self, guild, user, raid_id):
        self._cancel_operation(guild, user, 'raid_signup', raid_id)

    def _life_automation_enabled(self, guild, user, key, threshold):
        state = self.state(guild, user)
        config = state['config'].get(key, {})
        skill = self.life_skill(guild, user, key)
        return bool(config.get('enabled') and skill and skill['work'] >= threshold)

    def automates_fishing(self, guild, user, duration_id):
        threshold = {'short': 19, 'medium': 65, 'long': 133}.get(duration_id, 10**9)
        return self._life_automation_enabled(guild, user, 'fishing', threshold)

    def automates_farming(self, guild, user, location_id):
        threshold = FARMING_WORK_THRESHOLDS.get(location_id, 10**9)
        return self._life_automation_enabled(guild, user, 'farming', threshold)

    def fishing_notifications_due(self, fishing, now=None):
        from core.rpg_expeditions import is_doll_expedition_active
        return [row for row in fishing.notifications_due(now)
                if (not self.automates_fishing(row[0], row[1], row[3])
                    or is_doll_expedition_active(self.db, row[0], row[1], now))]

    def farming_notifications_due(self, farming, now=None):
        from core.rpg_expeditions import is_doll_expedition_active
        return [row for row in farming.notifications_due(now)
                if (not self.automates_farming(row[0], row[1], row[2])
                    or is_doll_expedition_active(self.db, row[0], row[1], now))]

    def auto_fishing_due(self, now=None):
        now = time.time() if now is None else now
        return self.db.execute('''SELECT guild_id,user_id,spot_id,duration_id,started_at
            FROM rpg_fishing_sessions WHERE status='active' AND ready_at<=?
            UNION SELECT s.guild_id,s.user_id,s.spot_id,s.duration_id,s.started_at
            FROM rpg_fishing_sessions s JOIN rpg_alchemy_operations o
            ON o.guild_id=s.guild_id AND o.user_id=s.user_id AND o.kind='fishing'
            AND o.source=CAST(s.started_at AS TEXT) AND o.status='reserved'
            WHERE s.status='claimed' ''', (now,)).fetchall()

    def auto_fish(self, fishing, guild, user, spot_id, duration_id, started_at, now=None):
        now = time.time() if now is None else now
        if not self.automates_fishing(guild, user, duration_id):
            return None
        source = str(float(started_at))
        receipt = self._reserve_operation(guild, user, 'fishing', source, 1, now)
        if receipt['status'] == 'completed':
            return receipt['result']
        saved = self.db.execute('''SELECT status,result FROM rpg_fishing_sessions
            WHERE guild_id=? AND user_id=? AND started_at=?''',
                                (guild, user, started_at)).fetchone()
        result = (json.loads(saved[1]) if saved and saved[0] == 'claimed'
                  else fishing.claim(guild, user, now=now, expected_started_at=started_at))
        restarted = False
        try:
            fishing.start(guild, user, spot_id, duration_id, now=now)
            restarted = True
        except CharacterError:
            # Collection already completed; preserve the result if restarting becomes invalid.
            restarted = False
        summary = {'result': result, 'restarted': restarted, 'fuel': receipt['fuel']}
        self._finish_operation(guild, user, 'fishing', source, summary)
        return summary

    def auto_farming_due(self, now=None):
        now = time.time() if now is None else now
        return self.db.execute('''SELECT guild_id,user_id,location_id,plant_id,planted_at
            FROM rpg_farming_sessions WHERE status='active' AND ready_at<=?
            UNION SELECT s.guild_id,s.user_id,s.location_id,s.plant_id,s.planted_at
            FROM rpg_farming_sessions s JOIN rpg_alchemy_operations o
            ON o.guild_id=s.guild_id AND o.user_id=s.user_id AND o.kind='farming'
            AND o.source=s.location_id||':'||CAST(s.planted_at AS TEXT) AND o.status='reserved'
            WHERE s.status='harvested'
            ORDER BY planted_at,location_id''', (now,)).fetchall()

    def auto_farm(self, farming, guild, user, location_id, plant_id, planted_at, now=None):
        now = time.time() if now is None else now
        if not self.automates_farming(guild, user, location_id):
            return None
        source = f'{location_id}:{float(planted_at)}'
        receipt = self._reserve_operation(guild, user, 'farming', source, 1, now)
        if receipt['status'] == 'completed':
            return receipt['result']
        saved = self.db.execute('''SELECT status,result FROM rpg_farming_sessions
            WHERE guild_id=? AND user_id=? AND location_id=? AND planted_at=?''',
                                (guild, user, location_id, planted_at)).fetchone()
        result = (json.loads(saved[1]) if saved and saved[0] == 'harvested'
                  else farming.harvest(guild, user, location_id, now=now,
                                       expected_planted_at=planted_at))
        replanted = False
        try:
            farming.plant(guild, user, location_id, plant_id, now=now)
            replanted = True
        except CharacterError:
            # Harvest already completed; preserve the result if replanting becomes invalid.
            replanted = False
        summary = {'result': result, 'replanted': replanted, 'fuel': receipt['fuel']}
        self._finish_operation(guild, user, 'farming', source, summary)
        return summary

    def rename(self, guild, user, name):
        name = ' '.join(str(name).split())[:16]
        if not name:
            raise CharacterError('人偶名稱不能留白。')
        with self.db:
            self._ensure(guild, user)
            self.db.execute('UPDATE rpg_alchemy_dolls SET name=? WHERE guild_id=? AND user_id=?',
                            (name, guild, user))

    def support(self, guild, user, *, require_enabled=True):
        """Build a per-battle support snapshot without adding another participant."""
        state = self.state(guild, user)
        body, core = state['active_body'], state['core']
        enabled = state['config'].get('combat_support', {}).get('enabled', False)
        if (require_enabled and not enabled) or not body or not core:
            return None
        from core.rpg_battle import DOLL_RARITY_STATS, DOLL_SKILL_IDS
        conditions = {'rally': ('self40', 'self'), 'repair': ('ally50', 'lowest'),
                      'group_repair': ('ally50', 'lowest'),
                      'cleanse': ('ally_debuff', 'debuffed'),
                      'barrier': ('ally50', 'lowest'), 'amplify': ('always', 'strongest'),
                      'interrupt': ('enemy_charging', 'boss'),
                      'counter': ('ally50', 'lowest')}
        skills = []
        for slot, saved in enumerate(core['skills']['combat'], 1):
            if not saved:
                continue
            rarity = DOLL_RARITY_STATS[saved['rarity']]
            condition, target = conditions.get(saved['key'], ('always', 'lowest'))
            skills.append(dict(slot=slot, key=saved['key'], rarity=saved['rarity'],
                               skill_id=DOLL_SKILL_IDS[saved['key'], saved['rarity']],
                               condition=condition, target=target,
                               maximum=rarity['uses'], remaining=rarity['uses']))
        if not skills:
            return None
        structure, power, durability, precision, spirit = body['stats']
        combat = {'HP': 50 + structure * 10, '攻擊': power * 3, '防禦': durability * 3,
                  '治療量': spirit * 3, '命中率': 95 + ACCURACY_BONUS[body['tier']],
                  '閃避率': 0, '暴擊率': 10}
        return dict(name=state['name'], stats=combat,
                    speed=min(100, 35 + precision // 10), skills=skills, ready_round=1)

    def prepare_support(self, guild, user, source, now=None):
        """Charge once for a battle and return its idempotent support snapshot."""
        now = time.time() if now is None else now
        source = str(source)
        existing = self.db.execute('''SELECT status,result FROM rpg_alchemy_operations
            WHERE guild_id=? AND user_id=? AND kind='battle_support' AND source=?''',
                                   (guild, user, source)).fetchone()
        if existing and existing[0] == 'completed' and existing[1]:
            return json.loads(existing[1]).get('support')
        snapshot = self.support(guild, user, require_enabled=not bool(existing))
        if not snapshot:
            return None
        try:
            receipt = self._reserve_operation(
                guild, user, 'battle_support', source, 1, now)
        except CharacterError:
            return None
        if receipt['status'] == 'completed' and receipt['result']:
            return receipt['result'].get('support')
        snapshot['fuel_cost'] = receipt['fuel']
        self._finish_operation(guild, user, 'battle_support', source,
                               {'support': snapshot, 'fuel': receipt['fuel']})
        return snapshot

__all__ = ['AlchemyDolls', 'BODY_BUDGETS', 'BODY_MATERIAL_TIERS',
           'COMBAT_SKILLS', 'COMBAT_SKILL_DETAILS',
           'LIFE_SKILLS', 'RARITIES', 'COMBAT_RARITY_STATS',
           'CORE_ITEM', 'POWDER_ITEM', 'body_material_id', 'material_profile',
           'parse_body_material', 'parse_stone', 'stone_id',
           'body_acceleration_cost', 'fuel_value', 'fuel_discount', 'operation_fuel_cost',
           'FUEL_CAPACITY', 'LIFE_WORK_UNLOCKS',
           'FARMING_WORK_THRESHOLDS', 'life_skill_unlocks', 'life_work',
           'raid_signup_pool']
