"""Free-form five-ingredient cooking, progression, meals, and legacy refunds."""
from collections import Counter
from dataclasses import dataclass
import json
import time
import uuid

from core.rpg import MAX_LEVEL, level_floor, level_for
from core.rpg_character import CharacterError, ITEMS


INGREDIENT_COUNT = 5
COOKING_XP_PER_QUALITY = 50
COOKING_INITIAL_XP_PERCENT = 25
DONATION_XP_PERCENT = 50
MAX_REWARDED_GUESTS = 3
MEAL_CLAIM_SECONDS = 30 * 60
MEAL_EFFECT_SECONDS = 24 * 60 * 60

GROWTH = '成長'
ASSAULT = '猛攻'
VITALITY = '活力'
FEAST = '盛宴'
NOURISHMENT = '滋養'
FORTUNE = '幸運'
AFTERTASTE = '餘韻'
EFFECT_TAGS = (GROWTH, ASSAULT, VITALITY, NOURISHMENT, FORTUNE)


@dataclass(frozen=True)
class Ingredient:
    tag: str
    quality: int
    aftertaste: int = 0


INGREDIENTS = {
    'fishing:pond:common': Ingredient(GROWTH, 1),
    'fishing:pond:rare': Ingredient(FORTUNE, 2, 1),
    'fishing:pond:weed': Ingredient(NOURISHMENT, 1),
    'fishing:lake:common': Ingredient(ASSAULT, 2),
    'fishing:lake:rare': Ingredient(FORTUNE, 3, 1),
    'fishing:lake:weed': Ingredient(NOURISHMENT, 2),
    'fishing:waterway:common': Ingredient(GROWTH, 3),
    'fishing:waterway:rare': Ingredient(FORTUNE, 4, 1),
    'fishing:waterway:weed': Ingredient(NOURISHMENT, 3),
    'farming:potato': Ingredient(FEAST, 1),
    'farming:dew_herb': Ingredient(VITALITY, 1),
    'farming:wheat': Ingredient(FEAST, 2),
    'farming:witch_tomato': Ingredient(VITALITY, 2),
    'farming:moonbell': Ingredient(ASSAULT, 2),
    'farming:chili': Ingredient(ASSAULT, 3),
    'farming:night_pumpkin': Ingredient(VITALITY, 3),
    'farming:dreammist_herb': Ingredient(GROWTH, 3),
    'farming:moonwhite_rice': Ingredient(FEAST, 4),
}

PAIRINGS = (
    ('fishing:pond:common', 'farming:potato'),
    ('fishing:pond:rare', 'farming:wheat'),
    ('fishing:pond:weed', 'farming:dew_herb'),
    ('fishing:lake:common', 'farming:witch_tomato'),
    ('fishing:lake:rare', 'farming:chili'),
    ('fishing:lake:weed', 'farming:moonbell'),
    ('fishing:waterway:common', 'farming:night_pumpkin'),
    ('fishing:waterway:rare', 'farming:moonwhite_rice'),
    ('fishing:waterway:weed', 'farming:dreammist_herb'),
)

GRADE_MULTIPLIERS = {'C': 100, 'B': 110, 'A': 125, 'S': 140}
GRADE_EFFECT_INDEX = {'C': 0, 'B': 1, 'A': 2, 'S': 3}
GRADE_NAMES = {'C': '家常', 'B': '美味', 'A': '精緻', 'S': '極上'}
TAG_NAMES = {
    GROWTH: '冒險者', ASSAULT: '豪快', VITALITY: '豐饒',
    NOURISHMENT: '滋養', FORTUNE: '幸運',
}


# Kept only for the one-time, lossless refund of the removed fixed recipes.
LEGACY_RECIPES = {
    'food:pond:common': ('fishing:pond:common', 'farming:potato'),
    'food:pond:rare': ('fishing:pond:rare', 'farming:wheat'),
    'food:lake:common': ('fishing:lake:common', 'farming:witch_tomato'),
    'food:lake:rare': ('fishing:lake:rare', 'farming:chili'),
    'food:waterway:common': ('fishing:waterway:common', 'farming:night_pumpkin'),
    'food:waterway:rare': ('fishing:waterway:rare', 'farming:moonwhite_rice'),
}
for tier, ingredients in (
        (1, ('fishing:pond:weed', 'farming:dew_herb')),
        (2, ('fishing:lake:weed', 'farming:moonbell')),
        (3, ('fishing:waterway:weed', 'farming:dreammist_herb'))):
    for kind in ('hp', 'attack', 'defense', 'healing', 'hit', 'evasion', 'critical'):
        LEGACY_RECIPES[f'potion:{tier}:{kind}'] = ingredients


def _grade(score):
    if score >= 85:
        return 'S'
    if score >= 70:
        return 'A'
    if score >= 55:
        return 'B'
    return 'C'


def _effect(tag, grade, secondary=False):
    index = GRADE_EFFECT_INDEX[grade]
    if tag == GROWTH:
        value = (5, 8, 11, 15)[index]
        return {'xp_percent': max(3, value // 2) if secondary else value}
    if tag == ASSAULT:
        attack = (4, 6, 8, 11)[index]
        critical = (2, 3, 4, 5)[index]
        if secondary:
            attack, critical = max(2, attack // 2), max(1, critical // 2)
        return {'attack_percent': attack, 'critical_points': critical}
    if tag == VITALITY:
        value = (4, 6, 8, 11)[index]
        value = max(2, value // 2) if secondary else value
        return {'hp_percent': value, 'healing_percent': value}
    if tag == NOURISHMENT:
        value = (2, 3, 4, 5)[index]
        return {'lifesteal_percent': max(1, value // 2) if secondary else value}
    if tag == FORTUNE:
        drop = (2, 3, 4, 5)[index]
        gold = (5, 8, 11, 15)[index]
        if secondary:
            drop, gold = min(2, max(1, drop // 2)), max(3, gold // 2)
        return {'drop_points': drop, 'gold_percent': gold}
    return {}


def effect_text(effect):
    parts = []
    labels = (
        ('xp_percent', '討伐 XP', '%'), ('attack_percent', '攻擊', '%'),
        ('critical_points', '暴擊率', ' 個百分點'), ('hp_percent', '最大 HP', '%'),
        ('healing_percent', '治療量', '%'), ('lifesteal_percent', '直接傷害吸血', '%'),
        ('drop_points', '掉落率', ' 個百分點'), ('gold_percent', '金幣', '%'),
    )
    for key, label, unit in labels:
        if effect.get(key):
            parts.append(f'{label} +{effect[key]}{unit}')
    return '、'.join(parts) or '只增加享用份數'


def guest_reward_target(capacity):
    """Number of distinct guests needed to unlock a public meal's remaining XP."""
    return min(MAX_REWARDED_GUESTS, max(0, capacity - 1))


def evaluate_ingredients(ingredient_ids, cooking_level=1):
    if len(ingredient_ids) != INGREDIENT_COUNT or any(key not in INGREDIENTS for key in ingredient_ids):
        raise CharacterError(f'料理必須選擇恰好 {INGREDIENT_COUNT} 份有效食材。')
    counts = Counter(ingredient_ids)
    tags = Counter()
    quality = 0
    aftertaste = 0
    first_tag = {}
    for index, key in enumerate(ingredient_ids):
        ingredient = INGREDIENTS[key]
        tags[ingredient.tag] += 1
        quality += ingredient.quality
        aftertaste += ingredient.aftertaste
        first_tag.setdefault(ingredient.tag, index)
    candidates = [tag for tag in EFFECT_TAGS if tags[tag]]
    if not candidates:
        raise CharacterError('至少需要一份能提供料理效果的魚、水草或藥草。')
    ordered = sorted(candidates, key=lambda tag: (-tags[tag], first_tag[tag]))
    primary = ordered[0]
    secondary = ordered[1] if cooking_level >= 20 and len(ordered) > 1 and tags[ordered[1]] >= 2 else None
    unique = len(counts)
    pairing_count = sum(left in counts and right in counts for left, right in PAIRINGS)
    mastery = min(10, max(0, cooking_level) // 10)
    score = min(100, 30 + quality * 2 + unique * 4 + pairing_count * 8 + mastery)
    grade = _grade(score)
    total_portions = min(8, 3 + tags[FEAST] + (1 if unique >= 4 else 0))
    duration = 3 if aftertaste >= 4 else 2 if aftertaste >= 2 else 1
    capacity = max(1, total_portions // duration)
    effect = _effect(primary, grade)
    if secondary:
        for key, value in _effect(secondary, grade, secondary=True).items():
            effect[key] = effect.get(key, 0) + value
    cooking_xp = quality * COOKING_XP_PER_QUALITY * GRADE_MULTIPLIERS[grade] // 100
    initial_cooking_xp = cooking_xp * COOKING_INITIAL_XP_PERCENT // 100
    immediate_cooking_xp = cooking_xp if capacity == 1 else initial_cooking_xp
    return {
        'ingredients': list(ingredient_ids), 'tag_counts': dict(tags), 'quality': quality,
        'unique': unique, 'pairings': pairing_count, 'score': score, 'grade': grade,
        'primary_tag': primary, 'secondary_tag': secondary, 'aftertaste': aftertaste,
        'total_portions': total_portions, 'duration': duration, 'capacity': capacity,
        'effect': effect, 'cooking_xp': cooking_xp,
        'initial_cooking_xp': initial_cooking_xp,
        'immediate_cooking_xp': immediate_cooking_xp,
        'name': f'{GRADE_NAMES[grade]}{TAG_NAMES[primary]}料理',
    }


class Provisions:
    """The historical name remains as the public cooking service attribute."""

    def __init__(self, store):
        self.store, self.db = store, store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_schema_migrations (
                name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_cooking_players (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_meals (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, host_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER, data TEXT NOT NULL,
                capacity INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_meal_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id TEXT NOT NULL,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, claimed_at REAL NOT NULL,
                valid_until REAL NOT NULL, remaining INTEGER NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_meal_uses (
                claim_id INTEGER NOT NULL, raid_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(claim_id,raid_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_meal_guest_rewards (
                meal_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY(meal_id,user_id))''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_meal_claims_active
                ON rpg_meal_claims(guild_id,user_id,valid_until,remaining)''')
            self._migrate_legacy_provisions()

    def _migrate_legacy_provisions(self):
        marker = 'freeform_cooking_v1'
        if self.db.execute('SELECT 1 FROM rpg_schema_migrations WHERE name=?', (marker,)).fetchone():
            return
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'rpg_inventory' in tables:
            for legacy, ingredients in LEGACY_RECIPES.items():
                rows = self.db.execute('''SELECT guild_id,user_id,quantity FROM rpg_inventory
                    WHERE item_id=? AND quantity>0''', (legacy,)).fetchall()
                for guild, user, quantity in rows:
                    for ingredient in ingredients:
                        self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                            VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                            DO UPDATE SET quantity=quantity+excluded.quantity''',
                                        (guild, user, ingredient, quantity))
                self.db.execute('DELETE FROM rpg_inventory WHERE item_id=?', (legacy,))
        if 'rpg_provision_loadouts' in tables:
            self.db.execute('DELETE FROM rpg_provision_loadouts')
        self.db.execute('INSERT INTO rpg_schema_migrations(name,applied_at) VALUES (?,?)',
                        (marker, int(time.time())))

    def state(self, guild, user):
        row = self.db.execute('SELECT xp FROM rpg_cooking_players WHERE guild_id=? AND user_id=?',
                              (guild, user)).fetchone()
        xp = row[0] if row else 0
        level = level_for(xp)
        return {'xp': xp, 'level': level, 'level_xp': xp - level_floor(level),
                'next_xp': None if level == MAX_LEVEL else level_floor(level + 1) - level_floor(level)}

    def preview(self, ingredient_ids, guild=None, user=None):
        level = self.state(guild, user)['level'] if guild is not None and user is not None else 1
        return evaluate_ingredients(ingredient_ids, level)

    def _active_meal(self, guild, user, now):
        return self.db.execute('''SELECT 1 FROM rpg_meal_claims
            WHERE guild_id=? AND user_id=? AND remaining>0 AND valid_until>? LIMIT 1''',
                               (guild, user, now)).fetchone()

    def active_effect(self, guild, user, now=None):
        now = time.time() if now is None else now
        row = self.db.execute('''SELECT c.meal_id,c.valid_until,c.remaining,m.data
            FROM rpg_meal_claims c JOIN rpg_meals m ON m.id=c.meal_id
            WHERE c.guild_id=? AND c.user_id=? AND c.remaining>0 AND c.valid_until>?
            ORDER BY c.claimed_at,c.id LIMIT 1''', (guild, user, now)).fetchone()
        if not row:
            return None
        meal_id, valid_until, remaining, raw = row
        data = json.loads(raw)
        return dict(meal_id=meal_id, valid_until=valid_until, remaining=remaining,
                    name=data['name'], grade=data['grade'],
                    primary_tag=data['primary_tag'], secondary_tag=data.get('secondary_tag'),
                    effect=data['effect'])

    def _host_has_open_table(self, guild, user, now):
        return self.db.execute('''SELECT 1 FROM rpg_meals m
            WHERE m.guild_id=? AND m.host_id=? AND
            (m.status='posting' OR (m.status='open' AND m.expires_at>? AND
             (SELECT COUNT(*) FROM rpg_meal_claims c WHERE c.meal_id=m.id)<m.capacity))
            LIMIT 1''', (guild, user, now)).fetchone()

    def cook(self, guild, user, channel, ingredient_ids, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.state(guild, user)
            data = evaluate_ingredients(ingredient_ids, state['level'])
            is_private = data['capacity'] == 1
            if is_private and self._active_meal(guild, user, now):
                raise CharacterError('你已有尚未使用完的料理效果，請先使用完再製作私人料理。')
            if not is_private and self._host_has_open_table(guild, user, now):
                raise CharacterError('你已有一桌尚未客滿的公開料理，請等待客滿或開桌時間結束。')
            required = Counter(ingredient_ids)
            counts = dict(self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
                WHERE guild_id=? AND user_id=?''', (guild, user)))
            missing = [f'{ITEMS[key].name} {counts.get(key, 0)}/{amount}'
                       for key, amount in required.items() if counts.get(key, 0) < amount]
            if missing:
                raise CharacterError('材料不足：' + '、'.join(missing))
            for key, amount in required.items():
                self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                    WHERE guild_id=? AND user_id=? AND item_id=?''', (amount, guild, user, key))
                self.db.execute('''DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=?
                    AND item_id=? AND quantity=0''', (guild, user, key))
            self.db.execute('''INSERT INTO rpg_cooking_players(guild_id,user_id,xp)
                VALUES (?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=xp+excluded.xp''',
                            (guild, user, data['immediate_cooking_xp']))
            meal_id = uuid.uuid4().hex
            status = 'private' if is_private else 'posting'
            offer = dict(id=meal_id, guild_id=guild, host_id=user, channel_id=channel,
                         message_id=None, data=data, capacity=data['capacity'], created_at=now,
                         expires_at=now + MEAL_CLAIM_SECONDS, status=status)
            self.db.execute('''INSERT INTO rpg_meals
                (id,guild_id,host_id,channel_id,message_id,data,capacity,created_at,expires_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                            (meal_id, guild, user, channel, None,
                             json.dumps(data, ensure_ascii=False, separators=(',', ':')),
                             data['capacity'], now, offer['expires_at'], status))
            if not self._active_meal(guild, user, now):
                self.db.execute('''INSERT INTO rpg_meal_claims
                    (meal_id,guild_id,user_id,claimed_at,valid_until,remaining)
                    VALUES (?,?,?,?,?,?)''',
                                (meal_id, guild, user, now, now + MEAL_EFFECT_SECONDS, data['duration']))
        return offer

    def donate(self, guild, user, ingredient_ids):
        if not ingredient_ids or any(key not in INGREDIENTS for key in ingredient_ids):
            raise CharacterError('請至少選擇一份有效的料理素材。')
        required = Counter(ingredient_ids)
        quality = sum(INGREDIENTS[key].quality * amount for key, amount in required.items())
        awarded_xp = quality * COOKING_XP_PER_QUALITY * DONATION_XP_PERCENT // 100
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            counts = dict(self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
                WHERE guild_id=? AND user_id=?''', (guild, user)))
            missing = [f'{ITEMS[key].name} {counts.get(key, 0)}/{amount}'
                       for key, amount in required.items() if counts.get(key, 0) < amount]
            if missing:
                raise CharacterError('材料不足：' + '、'.join(missing))
            for key, amount in required.items():
                self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                    WHERE guild_id=? AND user_id=? AND item_id=?''', (amount, guild, user, key))
                self.db.execute('''DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=?
                    AND item_id=? AND quantity=0''', (guild, user, key))
            self.db.execute('''INSERT INTO rpg_cooking_players(guild_id,user_id,xp)
                VALUES (?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=xp+excluded.xp''',
                            (guild, user, awarded_xp))
        return {'quantity': len(ingredient_ids), 'quality': quality, 'xp': awarded_xp}

    def publish(self, meal_id, message_id):
        with self.db:
            changed = self.db.execute("UPDATE rpg_meals SET message_id=?,status='open' "
                                      "WHERE id=? AND status='posting'", (message_id, meal_id))
            if not changed.rowcount:
                raise CharacterError('這桌料理已經失效。')
        return self.meal(meal_id)

    def cancel(self, meal_id, refund=False):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            meal = self.meal(meal_id)
            if not meal or meal['status'] in ('cancelled', 'refunded'):
                return meal
            self.db.execute('UPDATE rpg_meals SET status=? WHERE id=?',
                            ('refunded' if refund else 'cancelled', meal_id))
            if refund:
                for key, amount in Counter(meal['data']['ingredients']).items():
                    self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                        VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                        DO UPDATE SET quantity=quantity+excluded.quantity''',
                                    (meal['guild_id'], meal['host_id'], key, amount))
                self.db.execute('''UPDATE rpg_cooking_players SET xp=max(0,xp-?)
                    WHERE guild_id=? AND user_id=?''',
                                (meal['data'].get('immediate_cooking_xp',
                                                 meal['data']['initial_cooking_xp']),
                                 meal['guild_id'], meal['host_id']))
                self.db.execute('DELETE FROM rpg_meal_claims WHERE meal_id=?', (meal_id,))
        return self.meal(meal_id)

    def meal(self, meal_id):
        row = self.db.execute('''SELECT id,guild_id,host_id,channel_id,message_id,data,
            capacity,created_at,expires_at,status FROM rpg_meals WHERE id=?''', (meal_id,)).fetchone()
        if not row:
            return None
        keys = ('id', 'guild_id', 'host_id', 'channel_id', 'message_id', 'data',
                'capacity', 'created_at', 'expires_at', 'status')
        result = dict(zip(keys, row))
        result['data'] = json.loads(result['data'])
        return result

    def open_meals(self, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT id FROM rpg_meals WHERE status='open' "
                               'AND message_id IS NOT NULL AND expires_at>?', (now,)).fetchall()
        return [self.meal(row[0]) for row in rows]

    def recover_drafts(self):
        rows = self.db.execute(
            "SELECT id FROM rpg_meals WHERE status='posting' AND message_id IS NULL").fetchall()
        for (meal_id,) in rows:
            self.cancel(meal_id, refund=True)

    def claimants(self, meal_id):
        return [row[0] for row in self.db.execute('''SELECT user_id FROM rpg_meal_claims
            WHERE meal_id=? ORDER BY claimed_at,id''', (meal_id,)).fetchall()]

    def claim(self, meal_id, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            meal = self.meal(meal_id)
            if not meal or meal['guild_id'] != guild:
                raise CharacterError('找不到這桌料理。')
            if meal['status'] != 'open' or now >= meal['expires_at']:
                raise CharacterError('這桌料理已經結束了。')
            if self._active_meal(guild, user, now):
                raise CharacterError('你已經有尚未使用完的料理效果。')
            if len(self.claimants(meal_id)) >= meal['capacity']:
                raise CharacterError('這桌料理已經沒有空位了。')
            self.db.execute('''INSERT INTO rpg_meal_claims
                (meal_id,guild_id,user_id,claimed_at,valid_until,remaining)
                VALUES (?,?,?,?,?,?)''',
                            (meal_id, guild, user, now, now + MEAL_EFFECT_SECONDS,
                             meal['data']['duration']))
            if user != meal['host_id']:
                reward_target = guest_reward_target(meal['capacity'])
                rewarded_count = self.db.execute('''SELECT COUNT(*) FROM rpg_meal_guest_rewards
                    WHERE meal_id=?''', (meal_id,)).fetchone()[0]
                if rewarded_count < reward_target:
                    rewarded = self.db.execute('''INSERT OR IGNORE INTO rpg_meal_guest_rewards
                        (meal_id,user_id) VALUES (?,?)''', (meal_id, user))
                    if rewarded.rowcount:
                        remaining_xp = (meal['data']['cooking_xp']
                                        - meal['data']['initial_cooking_xp'])
                        bonus = (remaining_xp * (rewarded_count + 1) // reward_target
                                 - remaining_xp * rewarded_count // reward_target)
                        self.db.execute('''UPDATE rpg_cooking_players SET xp=xp+?
                            WHERE guild_id=? AND user_id=?''',
                                        (bonus, guild, meal['host_id']))
        return self.meal(meal_id)

    def prepare_for_raid(self, raid_id, guild, users, preserve_users=(), now=None):
        now = time.time() if now is None else now
        preserve_users = set(preserve_users)
        result = {}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for user in users:
                saved = self.db.execute('''SELECT u.data FROM rpg_meal_uses u
                    JOIN rpg_meal_claims c ON c.id=u.claim_id
                    WHERE u.raid_id=? AND c.guild_id=? AND c.user_id=? LIMIT 1''',
                                        (raid_id, guild, user)).fetchone()
                if saved:
                    result[user] = json.loads(saved[0])
                    continue
                row = self.db.execute('''SELECT c.id,c.meal_id,m.host_id,m.data FROM rpg_meal_claims c
                    JOIN rpg_meals m ON m.id=c.meal_id
                    WHERE c.guild_id=? AND c.user_id=? AND c.remaining>0 AND c.valid_until>?
                    ORDER BY c.claimed_at,c.id LIMIT 1''', (guild, user, now)).fetchone()
                if not row:
                    continue
                claim_id, meal_id, host_id, raw = row
                meal_data = json.loads(raw)
                payload = dict(meal_data['effect'], kind='meal', name=meal_data['name'],
                               grade=meal_data['grade'], primary_tag=meal_data['primary_tag'])
                encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                self.db.execute('INSERT INTO rpg_meal_uses VALUES (?,?,?)',
                                (claim_id, raid_id, encoded))
                if user not in preserve_users:
                    self.db.execute('UPDATE rpg_meal_claims SET remaining=remaining-1 WHERE id=?',
                                    (claim_id,))
                result[user] = payload
        return result
