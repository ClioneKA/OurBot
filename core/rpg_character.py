"""Character rules and transactional equipment storage; no Discord dependency."""
from dataclasses import dataclass, replace

from core.rpg import level_for


STAT_NAMES = ('生命力', '力氣', '耐力', '靈巧', '信仰')
COMBAT_NAMES = ('HP', '攻擊', '防禦', '治療量')
WEAPON_BONUSES = {'裝甲步兵': (10, 12, 2, 0), '騎士': (25, 5, 5, 0),
                  '弓兵': (0, 10, 0, 0), '僧侶': (0, 6, 0, 10)}
SUIT_BONUSES = {'裝甲步兵': (40, 2, 8, 0), '騎士': (60, 0, 12, 0),
                '弓兵': (25, 3, 5, 0), '僧侶': (25, 0, 5, 8)}
STABILITY = {'裝甲步兵': (60, 140), '騎士': (80, 120), '弓兵': (75, 125), '僧侶': (90, 110)}
# Every profession gains the same total points per level, with different priorities.
GROWTH = {
    '民兵': (2, 2, 2, 2, 2),
    '裝甲步兵': (2, 4, 2, 1, 1),
    '騎士': (4, 1, 3, 1, 1),
    '弓兵': (2, 3, 1, 3, 1),
    '僧侶': (2, 1, 1, 2, 4),
}
JOBS = tuple(job for job in GROWTH if job != '民兵')
PREFIXES = ('早期', '', '老練', '精銳')
WEAPONS = {'裝甲步兵': '戰斧', '騎士': '劍盾', '弓兵': '長弓', '僧侶': '權杖'}
SUITS = {'裝甲步兵': '步兵甲', '騎士': '騎士鎧', '弓兵': '獵裝', '僧侶': '僧袍'}


class CharacterError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    name: str
    slot: str
    job: str
    stage: int
    stats: tuple
    combat: tuple = (0, 0, 0, 0)
    stability: tuple = (100, 100)
    price: int = 0
    value: int = 0
    required_level: int | None = None
    party_bonus: bool = False


ITEMS = {}
ITEMS['starter:club'] = Item('木棒', '武器', '', 0, (0, 0, 0, 0, 0),
                              (0, 5, 5, 0), (60, 110))
for job in JOBS:
    for stage, prefix in enumerate(PREFIXES):
        for slot, names in (('武器', WEAPONS), ('套裝', SUITS)):
            weights = (WEAPON_BONUSES if slot == '武器' else SUIT_BONUSES)[job]
            key = f'{job}:{stage}:{slot}'
            ITEMS[key] = Item(prefix + names[job], slot, job, stage, (0, 0, 0, 0, 0),
                              tuple(weight * (stage + 1) for weight in weights),
                              STABILITY[job] if slot == '武器' else (100, 100),
                              (0, 500, 1500, 4000)[stage])
for index, name in enumerate(('生命護符', '力量指環', '堅韌徽章', '靈巧吊墜', '信仰念珠')):
    ITEMS[f'accessory:{index}'] = Item(name, '飾品', '', 0,
                                      tuple(3 if i == index else 0 for i in range(5)))

# Raid-only accessories are never included in profession supplies.
for index, name in enumerate(('魔物心核', '裂牙指環', '岩鱗徽章', '風羽吊墜', '星痕念珠')):
    ITEMS[f'raid:{index}'] = Item(name, '飾品', '', 0,
                                tuple(6 if i == index else 1 for i in range(5)))


# Golem-exclusive equipment: regular-stage requirements, no shop price or supplies.
ITEMS['golem:hammer'] = Item('鐵核重鎚', '武器', '裝甲步兵', 1, (0, 0, 0, 0, 0),
                            (20, 30, 4, 0), (50, 150))
ITEMS['golem:sword_shield'] = Item('鐵核劍盾', '武器', '騎士', 1, (0, 0, 0, 0, 0),
                                  (60, 12, 12, 0), (70, 130))
ITEMS['golem:bow'] = Item('鐵弦重弓', '武器', '弓兵', 1, (0, 0, 0, 0, 0),
                         (0, 26, 2, 0), (65, 135))
ITEMS['golem:staff'] = Item('鐵核祈禱杖', '武器', '僧侶', 1, (0, 0, 0, 0, 0),
                           (10, 14, 0, 26), (85, 115))


for job, key, name, bonuses in (
    ('裝甲步兵', 'infantry', '荊棘戰甲', (100, 6, 20, 0)),
    ('騎士', 'knight', '古木重鎧', (160, 0, 30, 0)),
    ('弓兵', 'archer', '藤葉獵裝', (70, 8, 12, 0)),
    ('僧侶', 'monk', '靈根僧袍', (70, 0, 12, 20)),
):
    ITEMS[f'tree:{key}'] = Item(name, '套裝', job, 1, (0, 0, 0, 0, 0), bonuses)


ITEMS['goblin:badge'] = Item('戰團徽章', '飾品', '', 1, (0, 0, 0, 0, 0),
                             required_level=20, party_bonus=True)
for job, key, name, bonuses in (
    ('裝甲步兵', 'axe', '掠奪者戰斧', (0, 60, 0, 0)),
    ('騎士', 'sword_shield', '掠奪者劍盾', (30, 40, 6, 0)),
    ('弓兵', 'bow', '掠奪者長弓', (0, 54, 0, 0)),
    ('僧侶', 'staff', '掠奪者權杖', (0, 42, 0, 20)),
):
    ITEMS[f'goblin:{key}'] = Item(name, '武器', job, 1, (0, 0, 0, 0, 0),
                                 bonuses, (40, 120), required_level=20)


for key, item in list(ITEMS.items()):
    if key.startswith('raid:'):
        ITEMS[key] = replace(item, value=300)
    elif key.startswith(('golem:', 'tree:', 'goblin:')):
        ITEMS[key] = replace(item, value=750)


def item_value(item):
    return item.price or item.value


def stage_for(level, settings):
    return sum(level >= threshold for threshold in
               (settings.regular_level, settings.veteran_level, settings.elite_level))


def stage_level(stage, settings):
    return (10, settings.regular_level, settings.veteran_level, settings.elite_level)[stage]


def stat_text(stats):
    return '、'.join(f'{name} +{value}' for name, value in zip(STAT_NAMES, stats) if value) or '無加成'


def item_level(item, settings):
    if item.required_level is not None:
        return item.required_level
    return stage_level(item.stage, settings) if item.job else 1


def combat_from_stats(total):
    vitality, strength, endurance, dexterity, faith = total
    return {'HP': 50 + vitality * 10, '攻擊': strength * 2 + faith,
            '防禦': endurance * 3, '治療量': faith * 3,
            '命中率': min(99, 75 + dexterity // 5),
            '閃避率': min(35, dexterity // 10), '暴擊率': min(50, 5 + dexterity // 8)}


def item_text(item):
    parts = [f'{name} +{value}' for name, value in zip(STAT_NAMES, item.stats) if value]
    parts += [f'{name} +{value}' for name, value in zip(COMBAT_NAMES, item.combat) if value]
    if item.slot == '武器':
        parts.append(f'穩定度 {item.stability[0]}–{item.stability[1]}%')
    if item.required_level is not None:
        parts.append(f'Lv.{item.required_level}')
    if item.party_bonus:
        parts.append('開戰每人（含自己）生命力／力氣／耐力／靈巧／信仰各 +1，最多各 +10，整場固定，僅自身')
    return '、'.join(parts) or '無加成'


class Characters:
    def __init__(self, store, settings):
        self.store = store
        self.db = store.db
        self.settings = settings
        # Separate tables leave all legacy XP and cooldown values intact.
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_characters (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, job TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_inventory (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, item_id))''')
            if 'quantity' not in {row[1] for row in self.db.execute('PRAGMA table_info(rpg_inventory)')}:
                self.db.execute('ALTER TABLE rpg_inventory ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_equipment (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, slot TEXT NOT NULL,
                item_id TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, slot),
                UNIQUE (guild_id, user_id, item_id))''')

    def job(self, guild_id, user_id):
        row = self.db.execute('SELECT job FROM rpg_characters WHERE guild_id=? AND user_id=?',
                              (guild_id, user_id)).fetchone()
        return row[0] if row else '民兵'

    def inventory(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        return [row[0] for row in self.db.execute(
            'SELECT item_id FROM rpg_inventory WHERE guild_id=? AND user_id=? ORDER BY item_id',
            (guild_id, user_id)) if row[0] in ITEMS]

    def ensure_starter(self, guild_id, user_id):
        if self.db.execute('SELECT 1 FROM rpg_inventory WHERE guild_id=? AND user_id=? AND item_id=?',
                           (guild_id, user_id, 'starter:club')).fetchone():
            return
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.ensure_starter(guild_id, user_id)
            return
        granted = self.db.execute('INSERT OR IGNORE INTO rpg_inventory(guild_id,user_id,item_id) VALUES (?,?,?)',
                                  (guild_id, user_id, 'starter:club'))
        if granted.rowcount:
            self.db.execute('INSERT OR IGNORE INTO rpg_equipment VALUES (?,?,?,?)',
                            (guild_id, user_id, '武器', 'starter:club'))

    def snapshot(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        level = level_for(self.store.xp(guild_id, user_id))
        job = self.job(guild_id, user_id)
        # Job is chosen explicitly at Lv.10; unchosen characters remain militia.
        stage = stage_for(level, self.settings) if job != '民兵' else 0
        capacity = 1 if job == '民兵' else stage + 2
        slots = ['武器', '套裝'] + [f'飾品{i}' for i in range(1, capacity + 1)]
        raw = dict(self.db.execute('SELECT slot, item_id FROM rpg_equipment WHERE guild_id=? AND user_id=?',
                                   (guild_id, user_id)))
        owned = set(self.inventory(guild_id, user_id))
        equipped = {}
        for slot, item_id in raw.items():
            item = ITEMS.get(item_id)
            if (slot in slots and item and item_id in owned and
                    (not item.job or item.job == job) and
                    level >= item_level(item, self.settings)):
                equipped[slot] = item_id
        # First nine level-ups always use militia growth, even after changing jobs.
        growth = GROWTH[job]
        base = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                     + (stage * weight * 2 if job != '民兵' else 0) for weight in growth)
        bonus = tuple(sum(ITEMS[item].stats[i] for item in equipped.values()) for i in range(5))
        total = tuple(a + b for a, b in zip(base, bonus))
        combat = combat_from_stats(total)
        combat_bonus = {name: sum(ITEMS[key].combat[i] for key in equipped.values())
                        for i, name in enumerate(COMBAT_NAMES)}
        for name, value in combat_bonus.items():
            combat[name] += value
        weapon = ITEMS.get(equipped.get('武器'))
        return dict(level=level, job=job, stage=stage, capacity=capacity, slots=slots,
                    title=job if job == '民兵' else PREFIXES[stage] + job,
                    base=base, bonus=bonus, total=total, combat=combat, equipped=equipped,
                    combat_bonus=combat_bonus, stability=weapon.stability if weapon else (100, 100))

    def inventory_counts(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        return dict(self.db.execute('SELECT item_id, quantity FROM rpg_inventory WHERE guild_id=? AND user_id=?',
                                    (guild_id, user_id)))

    def _grant(self, guild_id, user_id, job):
        candidates = [key for key, item in ITEMS.items()
                      if not key.startswith(('raid:', 'starter:', 'goblin:')) and
                      (not item.job or (item.job == job and item.stage == 0))]
        granted = []
        for key in candidates:
            cursor = self.db.execute('INSERT OR IGNORE INTO rpg_inventory(guild_id,user_id,item_id) VALUES (?, ?, ?)',
                                     (guild_id, user_id, key))
            if cursor.rowcount:
                granted.append(key)
        return granted

    def change_job(self, guild_id, user_id, job):
        if job not in JOBS:
            raise CharacterError('請選擇裝甲步兵、騎士、弓兵或僧侶。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.ensure_starter(guild_id, user_id)
            level = level_for(self.store.xp(guild_id, user_id))
            if level < 10:
                raise CharacterError('目前是民兵，達到 Lv.10 才能轉職。')
            if self.job(guild_id, user_id) == job:
                raise CharacterError('你已經是這個職業；進階裝備請從 /冒險 → 商店 購買取得。')
            self.db.execute('INSERT INTO rpg_characters VALUES (?, ?, ?) '
                            'ON CONFLICT(guild_id, user_id) DO UPDATE SET job=excluded.job',
                            (guild_id, user_id, job))
            self._grant(guild_id, user_id, job)
            self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            for slot in ('武器', '套裝'):
                self.db.execute('INSERT INTO rpg_equipment VALUES (?, ?, ?, ?)',
                                (guild_id, user_id, slot, f'{job}:0:{slot}'))
        return self.snapshot(guild_id, user_id)

    def claim(self, guild_id, user_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.snapshot(guild_id, user_id)
            if state['job'] == '民兵':
                raise CharacterError('達到 Lv.10 並使用 /冒險 → 轉職 後才能領取職業補給。')
            return self._grant(guild_id, user_id, state['job'])

    def equip(self, guild_id, user_id, item_id, accessory_slot=1):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.snapshot(guild_id, user_id)
            if item_id not in self.inventory(guild_id, user_id):
                raise CharacterError('背包中沒有這件裝備，請重新開啟 /冒險 → 裝備／能力 並從面板選擇。')
            item = ITEMS[item_id]
            if item.job and item.job != state['job']:
                raise CharacterError('這件裝備不適合目前職業。')
            if state['level'] < item_level(item, self.settings):
                raise CharacterError('等級尚未達到這件裝備的需求。')
            slot = item.slot
            if slot == '飾品':
                if not 1 <= accessory_slot <= state['capacity']:
                    raise CharacterError(f'目前只有 {state["capacity"]} 個飾品格。')
                slot = f'飾品{accessory_slot}'
            # Moving a unique accessory never duplicates its bonus.
            self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=? AND item_id=?',
                            (guild_id, user_id, item_id))
            self.db.execute('INSERT INTO rpg_equipment VALUES (?, ?, ?, ?) '
                            'ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET item_id=excluded.item_id',
                            (guild_id, user_id, slot, item_id))
        return slot

    def buy(self, guild_id, user_id, item_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            item = ITEMS.get(item_id)
            if not item or item.price <= 0:
                raise CharacterError('這件物品不在商店販售。')
            state = self.snapshot(guild_id, user_id)
            if item.job != state['job']:
                raise CharacterError('只能購買目前職業的裝備。')
            if state['level'] < stage_level(item.stage, self.settings):
                raise CharacterError('尚未達到這件裝備的等級需求。')
            if item_id in self.inventory(guild_id, user_id):
                raise CharacterError('你已經持有這件裝備，不需要重複購買。')
            paid = self.db.execute('UPDATE rpg_wallets SET gold=gold-? WHERE guild_id=? AND user_id=? AND gold>=?',
                                   (item.price, guild_id, user_id, item.price))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，需要 {item.price:,} 金幣。')
            self.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (?,?,?)', (guild_id, user_id, item_id))
        return item

    def unequip(self, guild_id, user_id, slot):
        if slot not in ('武器', '套裝', '飾品1', '飾品2', '飾品3', '飾品4', '飾品5'):
            raise CharacterError('無效的裝備欄位。')
        with self.db:
            cursor = self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=? AND slot=?',
                                     (guild_id, user_id, slot))
            if not cursor.rowcount:
                raise CharacterError('這個欄位沒有裝備。')

    def available_quantity(self, guild, user, key):
        owned = self.inventory_counts(guild, user).get(key, 0)
        equipped = self.db.execute('SELECT 1 FROM rpg_equipment WHERE guild_id=? AND user_id=? AND item_id=?',
                                   (guild, user, key)).fetchone()
        return max(0, owned - bool(equipped))

    def dispose(self, guild, user, key, quantity, recipient=None):
        """Transfer or sell only unequipped copies in one transaction."""
        item = ITEMS.get(key)
        if not item or item_value(item) <= 0:
            raise CharacterError('木棒與免費補給為綁定物品，不能給予或賣出。')
        if type(quantity) is not int or quantity < 1:
            raise CharacterError('數量必須是正整數。')
        if recipient == user:
            raise CharacterError('不能給予自己。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if quantity > self.available_quantity(guild, user, key):
                raise CharacterError('可用數量不足；正在穿戴的那一件不能給予或賣出，請先卸下。')
            self.db.execute('UPDATE rpg_inventory SET quantity=quantity-? WHERE guild_id=? AND user_id=? AND item_id=?',
                            (quantity, guild, user, key))
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? AND item_id=? AND quantity=0',
                            (guild, user, key))
            if recipient is not None:
                self.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity) VALUES (?,?,?,?) '
                                'ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity',
                                (guild, recipient, key, quantity))
                return 0
            gold = item_value(item) // 5 * quantity
            self.db.execute('INSERT INTO rpg_wallets VALUES (?,?,?) '
                            'ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold', (guild, user, gold))
            return gold
