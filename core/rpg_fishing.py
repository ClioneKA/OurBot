"""Persistent fishing progression, dispatches, crafting, and rewards."""
from collections import Counter
from dataclasses import dataclass
import json
import random
import time
import uuid

from core.rpg import MAX_LEVEL, level_floor, level_for
from core.rpg_character import CharacterError, ITEMS
from core.rpg_fishing_bosses import BOSS_CHANCE_PER_CATCH, FISHING_BOSSES


@dataclass(frozen=True)
class FishingSpot:
    name: str
    level: int
    base_xp: int
    loot: tuple
    rare_item: str


@dataclass(frozen=True)
class BigFish:
    name: str
    base_weight_g: int


DURATIONS = {
    'short': ('30 分鐘', 30 * 60, 2),
    'medium': ('2 小時', 2 * 60 * 60, 6),
    'long': ('8 小時', 8 * 60 * 60, 20),
}

SPOTS = {
    'pond': FishingSpot('中庭許願池', 1, 100, (
        ('fishing:pond:common', 52), ('fishing:pond:rare', 6),
        ('fishing:pond:weed', 18), ('fishing:pond:coin', 15),
        ('fishing:pond:rod', 3), ('fishing:pond:line', 3),
        ('fishing:pond:hook', 3)), 'fishing:pond:rare'),
    'lake': FishingSpot('魔女島湖泊', 20, 300, (
        ('fishing:lake:common', 50), ('fishing:lake:rare', 6),
        ('fishing:lake:weed', 20), ('fishing:lake:coin', 15),
        ('fishing:lake:rod', 3), ('fishing:lake:line', 3),
        ('fishing:lake:hook', 3)), 'fishing:lake:rare'),
    'waterway': FishingSpot('監獄地下水路', 40, 600, (
        ('fishing:waterway:common', 48), ('fishing:waterway:rare', 6),
        ('fishing:waterway:weed', 22), ('fishing:waterway:coin', 15),
        ('fishing:waterway:rod', 3), ('fishing:waterway:line', 3),
        ('fishing:waterway:hook', 3)), 'fishing:waterway:rare'),
    'bay': FishingSpot('魔女島海灣', 60, 1000, (
        ('fishing:bay:common', 48), ('fishing:bay:rare', 6),
        ('fishing:bay:weed', 22), ('fishing:bay:coin', 15),
        ('fishing:bay:rod', 3), ('fishing:bay:line', 3),
        ('fishing:bay:hook', 3)), 'fishing:bay:rare'),
}

BIG_FISH = {
    'pond': BigFish('百年池王鯉', 6_000),
    'lake': BigFish('魔女湖王鱒', 18_000),
    'waterway': BigFish('幽淵巨口魚', 30_000),
    'bay': BigFish('月潮巨鮪', 60_000),
}

WEIGHT_RANGES = {'short': (80, 110), 'medium': (90, 125), 'long': (100, 150)}
ROD_WEIGHT_FLOOR = {
    'fishing:rod:simple': 0, 'fishing:rod:magic': 5,
    'fishing:rod:glow': 10, 'fishing:rod:star_tide': 15,
}

ROD_BONUS = {
    'fishing:rod:old': (0.0, 1.0),
    'fishing:rod:simple': (0.2, 1.0),
    'fishing:rod:magic': (0.3, 1.1),
    'fishing:rod:glow': (0.4, 1.2),
    'fishing:rod:star_tide': (0.5, 1.2),
}

ROD_ORDER = tuple(ROD_BONUS)

RECIPES = {
    'fishing:rod:simple': ('fishing:rod:old', 'fishing:pond:rod',
                           'fishing:pond:line', 'fishing:pond:hook'),
    'fishing:rod:magic': ('fishing:rod:simple', 'fishing:lake:rod',
                          'fishing:lake:line', 'fishing:lake:hook'),
    'fishing:rod:glow': ('fishing:rod:magic', 'fishing:waterway:rod',
                         'fishing:waterway:line', 'fishing:waterway:hook'),
    'fishing:rod:star_tide': ('fishing:rod:glow', 'fishing:bay:rod',
                              'fishing:bay:line', 'fishing:bay:hook'),
}


def next_rod(rod_id):
    try:
        return ROD_ORDER[ROD_ORDER.index(rod_id) + 1]
    except (ValueError, IndexError):
        return None


def fishing_mastery(level, spot):
    return min(30, max(0, level - spot.level) // 10 * 10)


def _weighted_pick(loot, rare_item, rare_multiplier, rng):
    weighted = [(key, weight * rare_multiplier if key == rare_item else weight)
                for key, weight in loot]
    roll = rng.random() * sum(weight for _, weight in weighted)
    for key, weight in weighted:
        roll -= weight
        if roll < 0:
            return key
    return weighted[-1][0]


def _big_fish_weight(spot_id, duration_id, rod_id, rng):
    low, high = WEIGHT_RANGES[duration_id]
    low = min(high, low + ROD_WEIGHT_FLOOR.get(rod_id, 0))
    multiplier = low + int(rng.random() * (high - low + 1))
    return BIG_FISH[spot_id].base_weight_g * multiplier // 100


class Fishing:
    def __init__(self, store, rng=None, boss_rng=None):
        self.store, self.db = store, store.db
        self.rng = rng or random.Random()
        self.boss_rng = boss_rng or random.Random()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_encounters (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                spot_id TEXT NOT NULL, created_at REAL NOT NULL, raid_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued')''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_players (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
                rod_id TEXT NOT NULL DEFAULT 'fishing:rod:old',
                notify INTEGER NOT NULL DEFAULT 0 CHECK (notify IN (0,1)),
                display_fish_id TEXT,
                PRIMARY KEY (guild_id, user_id))''')
            player_columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_fishing_players)')}
            if 'display_fish_id' not in player_columns:
                self.db.execute('ALTER TABLE rpg_fishing_players ADD COLUMN display_fish_id TEXT')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_sessions (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                spot_id TEXT NOT NULL, duration_id TEXT NOT NULL,
                started_at REAL NOT NULL, ready_at REAL NOT NULL,
                rod_id TEXT NOT NULL, base_catches INTEGER NOT NULL,
                level_snapshot INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0,1)),
                result TEXT,
                PRIMARY KEY (guild_id, user_id))''')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_fishing_sessions)')}
            if 'level_snapshot' not in columns:
                self.db.execute('ALTER TABLE rpg_fishing_sessions ADD COLUMN level_snapshot INTEGER NOT NULL DEFAULT 1')
                self.db.execute("UPDATE rpg_fishing_sessions SET level_snapshot=20 WHERE spot_id='lake'")
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_records (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, fish_id TEXT NOT NULL,
                best_weight_g INTEGER NOT NULL CHECK(best_weight_g > 0),
                best_caught_at REAL NOT NULL, best_rod_id TEXT NOT NULL,
                best_duration_id TEXT NOT NULL, caught_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(guild_id,user_id,fish_id))''')

    def _ensure_player(self, guild, user):
        created = self.db.execute('INSERT OR IGNORE INTO rpg_fishing_players(guild_id,user_id) VALUES (?,?)',
                                  (guild, user)).rowcount
        if created:
            self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (?,?,?,1) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+1''', (guild, user, 'fishing:rod:old'))

    def state(self, guild, user):
        with self.db:
            self._ensure_player(guild, user)
        xp, rod, notify, display_fish_id = self.db.execute(
            'SELECT xp,rod_id,notify,display_fish_id FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
            (guild, user)).fetchone()
        row = self.db.execute('''SELECT spot_id,duration_id,started_at,ready_at,rod_id,
            base_catches,level_snapshot,status,result FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?''',
            (guild, user)).fetchone()
        session = None
        if row:
            session = dict(zip(('spot_id', 'duration_id', 'started_at', 'ready_at', 'rod_id',
                                'base_catches', 'level_snapshot', 'status', 'result'), row))
            if session['result']:
                session['result'] = json.loads(session['result'])
        return dict(xp=xp, level=level_for(xp), rod_id=rod, notify=bool(notify),
                    display_fish_id=display_fish_id, session=session)

    def records(self, guild, user):
        rows = self.db.execute('''SELECT fish_id,best_weight_g,best_caught_at,best_rod_id,
            best_duration_id,caught_count FROM rpg_fishing_records
            WHERE guild_id=? AND user_id=? ORDER BY best_caught_at''', (guild, user)).fetchall()
        return [dict(zip(('fish_id', 'best_weight_g', 'best_caught_at', 'best_rod_id',
                          'best_duration_id', 'caught_count'), row)) for row in rows]

    def display_record(self, guild, user):
        row = self.db.execute('''SELECT r.fish_id,r.best_weight_g,r.best_caught_at,
            r.best_rod_id,r.best_duration_id,r.caught_count
            FROM rpg_fishing_players p JOIN rpg_fishing_records r
              ON r.guild_id=p.guild_id AND r.user_id=p.user_id AND r.fish_id=p.display_fish_id
            WHERE p.guild_id=? AND p.user_id=?''', (guild, user)).fetchone()
        return (dict(zip(('fish_id', 'best_weight_g', 'best_caught_at', 'best_rod_id',
                          'best_duration_id', 'caught_count'), row)) if row else None)

    def set_display_fish(self, guild, user, fish_id):
        if fish_id is not None and fish_id not in BIG_FISH:
            raise CharacterError('請重新選擇展示漁獲。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            if fish_id is not None and not self.db.execute('''SELECT 1 FROM rpg_fishing_records
                    WHERE guild_id=? AND user_id=? AND fish_id=?''', (guild, user, fish_id)).fetchone():
                raise CharacterError('尚未捕獲這種大魚。')
            self.db.execute('''UPDATE rpg_fishing_players SET display_fish_id=?
                WHERE guild_id=? AND user_id=?''', (fish_id, guild, user))
        return fish_id

    def start(self, guild, user, spot_id, duration_id, now=None):
        now = time.time() if now is None else now
        if spot_id not in SPOTS or duration_id not in DURATIONS:
            raise CharacterError('請重新選擇釣場與釣魚時間。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            xp, rod = self.db.execute(
                'SELECT xp,rod_id FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
                (guild, user)).fetchone()
            level = level_for(xp)
            if level < SPOTS[spot_id].level:
                raise CharacterError(f'釣魚 Lv.{SPOTS[spot_id].level} 才能前往{SPOTS[spot_id].name}。')
            previous = self.db.execute(
                'SELECT status FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?',
                (guild, user)).fetchone()
            if previous and previous[0] == 'active':
                raise CharacterError('目前正在釣魚；完成後請先收竿。')
            label, seconds, catches = DURATIONS[duration_id]
            self.db.execute('DELETE FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?', (guild, user))
            self.db.execute('''INSERT INTO rpg_fishing_sessions
                (guild_id,user_id,spot_id,duration_id,started_at,ready_at,rod_id,base_catches,level_snapshot)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (guild, user, spot_id, duration_id, now, now + seconds, rod, catches, level))
        return dict(spot=SPOTS[spot_id], duration=label, ready_at=now + seconds,
                    rod_id=rod, base_catches=catches, level_snapshot=level,
                    mastery_percent=fishing_mastery(level, SPOTS[spot_id]))

    def claim(self, guild, user, now=None, expected_started_at=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT spot_id,duration_id,rod_id,base_catches,level_snapshot,started_at,ready_at,status,result
                FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
            if not row:
                raise CharacterError('目前沒有可以收竿的釣魚行程。')
            spot_id, duration_id, rod, base_catches, level_snapshot, started_at, ready_at, status, saved = row
            if expected_started_at is not None and started_at != expected_started_at:
                raise CharacterError('這則通知的釣魚行程已經結束，請查看目前的釣魚狀態。')
            if expected_started_at is not None and status == 'claimed':
                raise CharacterError('這次釣魚已經收竿，不能重複領取。')
            if status == 'claimed':
                result = json.loads(saved)
                result['replayed'] = True
                return result
            if status != 'active':
                raise CharacterError('這次釣魚已經中斷，無法收竿領取獎勵。')
            if now < ready_at:
                raise CharacterError('還沒釣完，時間結束後才能收竿。')
            spot = SPOTS[spot_id]
            bonus_chance, rare_multiplier = ROD_BONUS.get(rod, (0, 1))
            bonus = self.rng.random() < bonus_chance
            big_fish = None
            bonus_catch = bonus
            if bonus and rod in ROD_WEIGHT_FLOOR:
                big_fish_chance = min(0.4, base_catches * 0.02)
                if self.rng.random() < big_fish_chance:
                    weight = _big_fish_weight(spot_id, duration_id, rod, self.rng)
                    previous = self.db.execute('''SELECT best_weight_g FROM rpg_fishing_records
                        WHERE guild_id=? AND user_id=? AND fish_id=?''',
                                               (guild, user, spot_id)).fetchone()
                    is_record = not previous or weight > previous[0]
                    self.db.execute('''INSERT INTO rpg_fishing_records
                        (guild_id,user_id,fish_id,best_weight_g,best_caught_at,best_rod_id,
                         best_duration_id,caught_count) VALUES (?,?,?,?,?,?,?,1)
                        ON CONFLICT(guild_id,user_id,fish_id) DO UPDATE SET
                         caught_count=rpg_fishing_records.caught_count+1,
                         best_weight_g=CASE WHEN excluded.best_weight_g>best_weight_g
                            THEN excluded.best_weight_g ELSE best_weight_g END,
                         best_caught_at=CASE WHEN excluded.best_weight_g>best_weight_g
                            THEN excluded.best_caught_at ELSE best_caught_at END,
                         best_rod_id=CASE WHEN excluded.best_weight_g>best_weight_g
                            THEN excluded.best_rod_id ELSE best_rod_id END,
                         best_duration_id=CASE WHEN excluded.best_weight_g>best_weight_g
                            THEN excluded.best_duration_id ELSE best_duration_id END''',
                        (guild, user, spot_id, weight, now, rod, duration_id))
                    big_fish = dict(fish_id=spot_id, name=BIG_FISH[spot_id].name,
                                    weight_g=weight, is_record=is_record)
                    bonus_catch = False
            catches = base_catches + int(bonus_catch)
            caught = Counter()
            items = Counter()
            mastery_percent = fishing_mastery(level_snapshot, spot)
            mastery_bonus = 0
            for _ in range(catches):
                key = _weighted_pick(spot.loot, spot.rare_item, rare_multiplier, self.rng)
                caught[key] += 1
                items[key] += 1
                if mastery_percent and self.rng.random() * 100 < mastery_percent:
                    items[key] += 1
                    mastery_bonus += 1
            gained_xp = sum(count * (spot.base_xp * 3 // 2 if key == spot.rare_item else spot.base_xp)
                            for key, count in caught.items())
            old_xp = self.db.execute('SELECT xp FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
                                     (guild, user)).fetchone()[0]
            for key, count in items.items():
                self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                    VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                    DO UPDATE SET quantity=quantity+excluded.quantity''', (guild, user, key, count))
            self.db.execute('UPDATE rpg_fishing_players SET xp=xp+? WHERE guild_id=? AND user_id=?',
                            (gained_xp, guild, user))
            result = dict(spot_id=spot_id, items=dict(items), catches=catches, bonus=bonus,
                          bonus_catch=bonus_catch, big_fish=big_fish,
                          mastery_percent=mastery_percent, mastery_bonus=mastery_bonus,
                          xp=gained_xp, old_level=level_for(old_xp),
                          new_level=level_for(old_xp + gained_xp), replayed=False)
            # Separate RNG keeps encounter rolls independent of fish/rod quality.
            # Duplicate mastery items are not additional catches; a trophy is.
            trials = catches + int(big_fish is not None)
            if any(self.boss_rng.random() < BOSS_CHANCE_PER_CATCH for _ in range(trials)):
                encounter_id = uuid.uuid4().hex
                self.db.execute('''INSERT INTO rpg_fishing_encounters
                    (id,guild_id,user_id,spot_id,created_at) VALUES (?,?,?,?,?)''',
                    (encounter_id, guild, user, spot_id, now))
                result['boss_encounter'] = dict(id=encounter_id, name=FISHING_BOSSES[spot_id].name)
            self.db.execute('''UPDATE rpg_fishing_sessions SET status='claimed',result=?
                WHERE guild_id=? AND user_id=?''',
                (json.dumps(result, ensure_ascii=False, separators=(',', ':')), guild, user))
            return result

    def cancel(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT spot_id,duration_id FROM rpg_fishing_sessions
                WHERE guild_id=? AND user_id=? AND status='active' ''',
                (guild, user)).fetchone()
            if not row:
                raise CharacterError('目前沒有進行中的釣魚行程。')
            self.db.execute('''UPDATE rpg_fishing_sessions
                SET status='cancelled',result=NULL WHERE guild_id=? AND user_id=?''',
                (guild, user))
        spot_id, duration_id = row
        return dict(spot_id=spot_id, duration_id=duration_id)

    def equip(self, guild, user, rod_id):
        if rod_id not in ROD_BONUS:
            raise CharacterError('請重新選擇釣竿。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            if not self.db.execute('''SELECT 1 FROM rpg_inventory
                    WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>0''',
                    (guild, user, rod_id)).fetchone():
                raise CharacterError('背包中沒有這支釣竿。')
            self.db.execute('UPDATE rpg_fishing_players SET rod_id=? WHERE guild_id=? AND user_id=?',
                            (rod_id, guild, user))
        return ITEMS[rod_id]

    def craft_next(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            current = self.db.execute('SELECT rod_id FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
                                      (guild, user)).fetchone()[0]
            target = next_rod(current)
            if not target:
                raise CharacterError(f'{ITEMS[current].name}已是目前最高階釣竿。')
            counts = dict(self.db.execute('SELECT item_id,quantity FROM rpg_inventory WHERE guild_id=? AND user_id=?',
                                          (guild, user)))
            missing = [ITEMS[key].name for key in RECIPES[target] if counts.get(key, 0) < 1]
            if missing:
                raise CharacterError('缺少材料：' + '、'.join(missing))
            for key in RECIPES[target]:
                self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                    WHERE guild_id=? AND user_id=? AND item_id=?''', (guild, user, key))
                self.db.execute('''DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=?
                    AND item_id=? AND quantity=0''', (guild, user, key))
            self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (?,?,?,1) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+1''', (guild, user, target))
            self.db.execute('UPDATE rpg_fishing_players SET rod_id=? WHERE guild_id=? AND user_id=?',
                            (target, guild, user))
        return ITEMS[target]

    def set_notify(self, guild, user, enabled):
        with self.db:
            self._ensure_player(guild, user)
            self.db.execute('UPDATE rpg_fishing_players SET notify=? WHERE guild_id=? AND user_id=?',
                            (int(enabled), guild, user))
        return enabled

    def notifications_due(self, now=None):
        now = time.time() if now is None else now
        return self.db.execute('''SELECT s.guild_id,s.user_id,s.spot_id,s.duration_id,s.started_at
            FROM rpg_fishing_sessions s JOIN rpg_fishing_players p
            ON p.guild_id=s.guild_id AND p.user_id=s.user_id
            WHERE s.status='active' AND s.ready_at<=? AND s.notified=0 AND p.notify=1''', (now,)).fetchall()

    def reserve_notification(self, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            cursor = self.db.execute('''UPDATE rpg_fishing_sessions SET notified=1
                WHERE guild_id=? AND user_id=? AND status='active' AND ready_at<=? AND notified=0
                AND EXISTS (SELECT 1 FROM rpg_fishing_players p WHERE p.guild_id=? AND p.user_id=? AND p.notify=1)''',
                (guild, user, now, guild, user))
        return bool(cursor.rowcount)

    def notified_active(self):
        return self.db.execute('''SELECT guild_id,user_id,spot_id,duration_id,started_at
            FROM rpg_fishing_sessions WHERE status='active' AND notified=1''').fetchall()


def fishing_progress(xp):
    level = level_for(xp)
    if level == MAX_LEVEL:
        return level, xp - level_floor(level), None
    return level, xp - level_floor(level), level_floor(level + 1) - level_floor(level)
