"""Persistent fishing progression, dispatches, crafting, and rewards."""
from collections import Counter
from dataclasses import dataclass
import json
import random
import time

from core.rpg import MAX_LEVEL, level_floor, level_for
from core.rpg_character import CharacterError, ITEMS


@dataclass(frozen=True)
class FishingSpot:
    name: str
    level: int
    base_xp: int
    loot: tuple
    rare_item: str


DURATIONS = {
    'short': ('30 分鐘', 30 * 60, 2),
    'medium': ('2 小時', 2 * 60 * 60, 6),
    'long': ('8 小時', 8 * 60 * 60, 20),
}

SPOTS = {
    'pond': FishingSpot('中庭許願池', 1, 100, (
        ('fishing:pond:common', 54), ('fishing:pond:rare', 4),
        ('fishing:pond:weed', 18), ('fishing:pond:coin', 15),
        ('fishing:pond:rod', 3), ('fishing:pond:line', 3),
        ('fishing:pond:hook', 3)), 'fishing:pond:rare'),
    'lake': FishingSpot('魔女島湖泊', 20, 300, (
        ('fishing:lake:common', 49), ('fishing:lake:rare', 7),
        ('fishing:lake:weed', 20), ('fishing:lake:coin', 15),
        ('fishing:lake:rod', 3), ('fishing:lake:line', 3),
        ('fishing:lake:hook', 3)), 'fishing:lake:rare'),
}

ROD_BONUS = {
    'fishing:rod:old': (0.0, 1.0),
    'fishing:rod:simple': (0.2, 1.0),
    'fishing:rod:magic': (0.3, 1.1),
}

RECIPES = {
    'fishing:rod:simple': ('fishing:rod:old', 'fishing:pond:rod',
                           'fishing:pond:line', 'fishing:pond:hook'),
    'fishing:rod:magic': ('fishing:rod:simple', 'fishing:lake:rod',
                          'fishing:lake:line', 'fishing:lake:hook'),
}


def _weighted_pick(loot, rare_item, rare_multiplier, rng):
    weighted = [(key, weight * rare_multiplier if key == rare_item else weight)
                for key, weight in loot]
    roll = rng.random() * sum(weight for _, weight in weighted)
    for key, weight in weighted:
        roll -= weight
        if roll < 0:
            return key
    return weighted[-1][0]


class Fishing:
    def __init__(self, store, rng=None):
        self.store, self.db = store, store.db
        self.rng = rng or random.Random()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_players (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
                rod_id TEXT NOT NULL DEFAULT 'fishing:rod:old',
                notify INTEGER NOT NULL DEFAULT 0 CHECK (notify IN (0,1)),
                PRIMARY KEY (guild_id, user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_fishing_sessions (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                spot_id TEXT NOT NULL, duration_id TEXT NOT NULL,
                started_at REAL NOT NULL, ready_at REAL NOT NULL,
                rod_id TEXT NOT NULL, base_catches INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0,1)),
                result TEXT,
                PRIMARY KEY (guild_id, user_id))''')

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
        xp, rod, notify = self.db.execute(
            'SELECT xp,rod_id,notify FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
            (guild, user)).fetchone()
        row = self.db.execute('''SELECT spot_id,duration_id,started_at,ready_at,rod_id,
            base_catches,status,result FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?''',
            (guild, user)).fetchone()
        session = None
        if row:
            session = dict(zip(('spot_id', 'duration_id', 'started_at', 'ready_at', 'rod_id',
                                'base_catches', 'status', 'result'), row))
            if session['result']:
                session['result'] = json.loads(session['result'])
        return dict(xp=xp, level=level_for(xp), rod_id=rod, notify=bool(notify), session=session)

    def start(self, guild, user, spot_id, duration_id, now=None):
        now = time.time() if now is None else now
        if spot_id not in SPOTS or duration_id not in DURATIONS:
            raise CharacterError('請重新選擇釣場與派遣時間。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            xp, rod = self.db.execute(
                'SELECT xp,rod_id FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
                (guild, user)).fetchone()
            if level_for(xp) < SPOTS[spot_id].level:
                raise CharacterError(f'釣魚 Lv.{SPOTS[spot_id].level} 才能前往{SPOTS[spot_id].name}。')
            previous = self.db.execute(
                'SELECT status FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?',
                (guild, user)).fetchone()
            if previous and previous[0] == 'active':
                raise CharacterError('目前已有釣魚派遣；完成後請先收竿。')
            label, seconds, catches = DURATIONS[duration_id]
            self.db.execute('DELETE FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?', (guild, user))
            self.db.execute('''INSERT INTO rpg_fishing_sessions
                (guild_id,user_id,spot_id,duration_id,started_at,ready_at,rod_id,base_catches)
                VALUES (?,?,?,?,?,?,?,?)''',
                (guild, user, spot_id, duration_id, now, now + seconds, rod, catches))
        return dict(spot=SPOTS[spot_id], duration=label, ready_at=now + seconds,
                    rod_id=rod, base_catches=catches)

    def claim(self, guild, user, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT spot_id,rod_id,base_catches,ready_at,status,result
                FROM rpg_fishing_sessions WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
            if not row:
                raise CharacterError('目前沒有可以收竿的釣魚派遣。')
            spot_id, rod, base_catches, ready_at, status, saved = row
            if status == 'claimed':
                result = json.loads(saved)
                result['replayed'] = True
                return result
            if now < ready_at:
                raise CharacterError('魚還沒有上鉤，派遣完成後才能收竿。')
            spot = SPOTS[spot_id]
            bonus_chance, rare_multiplier = ROD_BONUS.get(rod, (0, 1))
            bonus = self.rng.random() < bonus_chance
            catches = base_catches + int(bonus)
            items = Counter(_weighted_pick(spot.loot, spot.rare_item, rare_multiplier, self.rng)
                            for _ in range(catches))
            gained_xp = sum(count * (spot.base_xp * 3 // 2 if key == spot.rare_item else spot.base_xp)
                            for key, count in items.items())
            old_xp = self.db.execute('SELECT xp FROM rpg_fishing_players WHERE guild_id=? AND user_id=?',
                                     (guild, user)).fetchone()[0]
            for key, count in items.items():
                self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                    VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                    DO UPDATE SET quantity=quantity+excluded.quantity''', (guild, user, key, count))
            self.db.execute('UPDATE rpg_fishing_players SET xp=xp+? WHERE guild_id=? AND user_id=?',
                            (gained_xp, guild, user))
            result = dict(spot_id=spot_id, items=dict(items), catches=catches, bonus=bonus,
                          xp=gained_xp, old_level=level_for(old_xp),
                          new_level=level_for(old_xp + gained_xp), replayed=False)
            self.db.execute('''UPDATE rpg_fishing_sessions SET status='claimed',result=?
                WHERE guild_id=? AND user_id=?''',
                (json.dumps(result, ensure_ascii=False, separators=(',', ':')), guild, user))
            return result

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
            target = ('fishing:rod:simple' if current == 'fishing:rod:old' else
                      'fishing:rod:magic' if current == 'fishing:rod:simple' else None)
            if not target:
                raise CharacterError('魔力釣竿已是目前最高階釣竿。')
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
        return self.db.execute('''SELECT s.guild_id,s.user_id,s.spot_id
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


def fishing_progress(xp):
    level = level_for(xp)
    if level == MAX_LEVEL:
        return level, xp - level_floor(level), None
    return level, xp - level_floor(level), level_floor(level + 1) - level_floor(level)
