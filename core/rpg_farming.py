"""Persistent farming plots, crop unlocks, harvest yields, and XP."""
from dataclasses import dataclass
import json
import random
import time

from core.rpg import MAX_LEVEL, level_floor, level_for
from core.rpg_character import CharacterError


@dataclass(frozen=True)
class Plant:
    name: str
    item_id: str
    level: int
    seconds: int
    base_yield: int
    xp_each: int
    role: str


LOCATIONS = {
    'courtyard': '中庭花圃',
    'prison': '監獄菜園',
}

LOCATION_LEVELS = {
    'courtyard': 1,
    'prison': 20,
}

PLANTS = {
    'potato': Plant('馬鈴薯', 'farming:potato', 1, 60 * 60, 2, 100, '搭配普通魚'),
    'dew_herb': Plant('晨露藥草', 'farming:dew_herb', 5, 2 * 60 * 60, 2, 200, '製作初級藥水'),
    'wheat': Plant('小麥', 'farming:wheat', 10, 4 * 60 * 60, 1, 800, '搭配稀有魚'),
    'witch_tomato': Plant('魔女番茄', 'farming:witch_tomato', 20, 60 * 60, 2, 300, '搭配普通魚'),
    'moonbell': Plant('月鈴草', 'farming:moonbell', 25, 2 * 60 * 60, 2, 600, '製作中級藥水'),
    'chili': Plant('火紅辣椒', 'farming:chili', 30, 4 * 60 * 60, 1, 2400, '搭配稀有魚'),
}


def growth_text(seconds):
    return f'{seconds // 3600} 小時' if seconds % 3600 == 0 else f'{seconds // 60} 分鐘'


class Farming:
    def __init__(self, store, rng=None):
        self.store, self.db = store, store.db
        self.rng = rng or random.Random()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_farming_players (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
                notify INTEGER NOT NULL DEFAULT 0 CHECK (notify IN (0,1)),
                PRIMARY KEY (guild_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_farming_sessions (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, location_id TEXT NOT NULL,
                plant_id TEXT NOT NULL, planted_at REAL NOT NULL, ready_at REAL NOT NULL,
                level_snapshot INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active', result TEXT,
                notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0,1)),
                PRIMARY KEY (guild_id,user_id,location_id))''')
            player_columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_farming_players)')}
            if 'notify' not in player_columns:
                self.db.execute('ALTER TABLE rpg_farming_players ADD COLUMN notify INTEGER NOT NULL DEFAULT 0')
            session_columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_farming_sessions)')}
            if 'notified' not in session_columns:
                self.db.execute('ALTER TABLE rpg_farming_sessions ADD COLUMN notified INTEGER NOT NULL DEFAULT 0')

    def _ensure_player(self, guild, user):
        self.db.execute('INSERT OR IGNORE INTO rpg_farming_players(guild_id,user_id) VALUES (?,?)',
                        (guild, user))

    def state(self, guild, user):
        with self.db:
            self._ensure_player(guild, user)
        xp, notify = self.db.execute('SELECT xp,notify FROM rpg_farming_players WHERE guild_id=? AND user_id=?',
                                     (guild, user)).fetchone()
        sessions = {}
        for row in self.db.execute('''SELECT location_id,plant_id,planted_at,ready_at,
                level_snapshot,status,result,notified FROM rpg_farming_sessions
                WHERE guild_id=? AND user_id=?''', (guild, user)):
            location, plant, planted, ready, level, status, result, notified = row
            sessions[location] = dict(plant_id=plant, planted_at=planted, ready_at=ready,
                                      level_snapshot=level, status=status,
                                      result=json.loads(result) if result else None,
                                      notified=bool(notified))
        return dict(xp=xp, level=level_for(xp), notify=bool(notify), sessions=sessions)

    def plant(self, guild, user, location_id, plant_id, now=None):
        now = time.time() if now is None else now
        if location_id not in LOCATIONS or plant_id not in PLANTS:
            raise CharacterError('請重新選擇農耕地點與植物。')
        crop = PLANTS[plant_id]
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._ensure_player(guild, user)
            xp = self.db.execute('SELECT xp FROM rpg_farming_players WHERE guild_id=? AND user_id=?',
                                 (guild, user)).fetchone()[0]
            level = level_for(xp)
            required_level = LOCATION_LEVELS[location_id]
            if level < required_level:
                raise CharacterError(f'農耕 Lv.{required_level} 才能使用{LOCATIONS[location_id]}。')
            if level < crop.level:
                raise CharacterError(f'農耕 Lv.{crop.level} 才能種植{crop.name}。')
            row = self.db.execute('''SELECT status FROM rpg_farming_sessions
                WHERE guild_id=? AND user_id=? AND location_id=?''',
                (guild, user, location_id)).fetchone()
            if row and row[0] == 'active':
                raise CharacterError(f'{LOCATIONS[location_id]}已有植物，成熟後請先收成。')
            self.db.execute('''DELETE FROM rpg_farming_sessions
                WHERE guild_id=? AND user_id=? AND location_id=?''', (guild, user, location_id))
            self.db.execute('''INSERT INTO rpg_farming_sessions
                (guild_id,user_id,location_id,plant_id,planted_at,ready_at,level_snapshot)
                VALUES (?,?,?,?,?,?,?)''',
                (guild, user, location_id, plant_id, now, now + crop.seconds, level))
        return dict(location=LOCATIONS[location_id], plant=crop, ready_at=now + crop.seconds,
                    level_snapshot=level)

    def harvest(self, guild, user, location_id, now=None, expected_planted_at=None):
        now = time.time() if now is None else now
        if location_id not in LOCATIONS:
            raise CharacterError('請重新選擇農耕地點。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT plant_id,planted_at,ready_at,level_snapshot,status,result
                FROM rpg_farming_sessions WHERE guild_id=? AND user_id=? AND location_id=?''',
                (guild, user, location_id)).fetchone()
            if not row:
                raise CharacterError(f'{LOCATIONS[location_id]}目前沒有可以收成的植物。')
            plant_id, planted_at, ready_at, planted_level, status, saved = row
            if expected_planted_at is not None and planted_at != expected_planted_at:
                raise CharacterError('這則通知的植物已經收成，請查看目前的農耕狀態。')
            if expected_planted_at is not None and status == 'harvested':
                raise CharacterError('這批植物已經收成，不能重複領取。')
            if status == 'harvested':
                result = json.loads(saved)
                result['replayed'] = True
                return result
            if now < ready_at:
                raise CharacterError('植物尚未成熟。')
            crop = PLANTS[plant_id]
            difference = max(0, planted_level - crop.level)
            guaranteed = min(3, difference // 10)
            chance = 0 if guaranteed == 3 else difference % 10 / 10
            lucky = chance > 0 and self.rng.random() < chance
            quantity = crop.base_yield + guaranteed + int(lucky)
            gained_xp = quantity * crop.xp_each
            old_xp = self.db.execute('SELECT xp FROM rpg_farming_players WHERE guild_id=? AND user_id=?',
                                     (guild, user)).fetchone()[0]
            self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+excluded.quantity''',
                (guild, user, crop.item_id, quantity))
            self.db.execute('UPDATE rpg_farming_players SET xp=xp+? WHERE guild_id=? AND user_id=?',
                            (gained_xp, guild, user))
            result = dict(location_id=location_id, plant_id=plant_id, quantity=quantity,
                          base_yield=crop.base_yield, level_bonus=guaranteed + int(lucky),
                          lucky=lucky, xp=gained_xp, old_level=level_for(old_xp),
                          new_level=level_for(old_xp + gained_xp), replayed=False)
            self.db.execute('''UPDATE rpg_farming_sessions SET status='harvested',result=?
                WHERE guild_id=? AND user_id=? AND location_id=?''',
                (json.dumps(result, ensure_ascii=False, separators=(',', ':')),
                 guild, user, location_id))
            return result

    def set_notify(self, guild, user, enabled):
        with self.db:
            self._ensure_player(guild, user)
            self.db.execute('UPDATE rpg_farming_players SET notify=? WHERE guild_id=? AND user_id=?',
                            (int(enabled), guild, user))
        return enabled

    def notifications_due(self, now=None):
        now = time.time() if now is None else now
        return self.db.execute('''SELECT s.guild_id,s.user_id,s.location_id,s.plant_id,s.planted_at
            FROM rpg_farming_sessions s JOIN rpg_farming_players p
            ON p.guild_id=s.guild_id AND p.user_id=s.user_id
            WHERE s.status='active' AND s.ready_at<=? AND s.notified=0 AND p.notify=1''', (now,)).fetchall()

    def reserve_notification(self, guild, user, location_id, now=None):
        now = time.time() if now is None else now
        with self.db:
            cursor = self.db.execute('''UPDATE rpg_farming_sessions SET notified=1
                WHERE guild_id=? AND user_id=? AND location_id=? AND status='active'
                AND ready_at<=? AND notified=0 AND EXISTS (
                    SELECT 1 FROM rpg_farming_players p
                    WHERE p.guild_id=? AND p.user_id=? AND p.notify=1)''',
                (guild, user, location_id, now, guild, user))
        return bool(cursor.rowcount)

    def notified_active(self):
        return self.db.execute('''SELECT guild_id,user_id,location_id,plant_id,planted_at
            FROM rpg_farming_sessions WHERE status='active' AND notified=1''').fetchall()


def farming_progress(xp):
    level = level_for(xp)
    if level == MAX_LEVEL:
        return level, xp - level_floor(level), None
    return level, xp - level_floor(level), level_floor(level + 1) - level_floor(level)
