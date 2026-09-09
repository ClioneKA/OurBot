"""Persistent server-local XP and voice participation accounting."""
from bisect import bisect_right
from math import floor
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timedelta, timezone


MAX_LEVEL = 120


def _experience_thresholds():
    # Standard RuneScape curve (not Invention): floor each term BEFORE summing.
    # https://runescape.wiki/w/Experience#Equations
    total = 0
    thresholds = [0]
    for previous_level in range(1, MAX_LEVEL):
        total += floor(previous_level + 300 * 2 ** (previous_level / 7))
        thresholds.append(total // 4)
    return tuple(thresholds)


XP_THRESHOLDS = _experience_thresholds()


def level_for(xp):
    if xp < 0:
        raise ValueError('XP must be nonnegative')
    return bisect_right(XP_THRESHOLDS, xp)


def level_floor(level):
    if not 1 <= level <= MAX_LEVEL:
        raise ValueError(f'Level must be between 1 and {MAX_LEVEL}')
    return XP_THRESHOLDS[level - 1]


def scaled_chat_xp(base, xp):
    """Cube-root growth relative to Lv.10, rounded down with exact integers.

    At the level cap reuse the final available level gap (119 -> 120).
    """
    level = min(level_for(xp), MAX_LEVEL - 1)
    required = max(level_floor(level + 1) - level_floor(level),
                   level_floor(11) - level_floor(10))
    baseline = level_floor(11) - level_floor(10)
    target = base ** 3 * required // baseline
    low, high = 0, max(1, base)
    while high ** 3 <= target:
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if middle ** 3 <= target:
            low = middle
        else:
            high = middle
    return low


def title_for(level):
    for threshold, title in ((30, '傳說英雄'), (20, '精英冒險者'),
                             (10, '資深冒險者'), (5, '見習冒險者')):
        if level >= threshold:
            return title
    return '初心者'


class RPGStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS players (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            last_text_at REAL,
            PRIMARY KEY (guild_id, user_id))''')
        self.db.execute('CREATE INDEX IF NOT EXISTS players_ranking ON players (guild_id, xp DESC, user_id)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_wallets (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            gold INTEGER NOT NULL DEFAULT 0 CHECK (gold >= 0),
            PRIMARY KEY (guild_id, user_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_divinations (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            day TEXT NOT NULL, draws INTEGER NOT NULL DEFAULT 0,
            card TEXT, bound_raid_id TEXT, summon_raid_id TEXT,
            PRIMARY KEY(guild_id,user_id))''')
        self.db.commit()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_daily_xp (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
                source TEXT NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id,day,source))''')

    def close(self):
        self.db.close()

    def gold(self, guild_id, user_id):
        row = self.db.execute('SELECT gold FROM rpg_wallets WHERE guild_id=? AND user_id=?',
                              (guild_id, user_id)).fetchone()
        return row[0] if row else 0

    def xp(self, guild_id, user_id):
        row = self.db.execute('SELECT xp FROM players WHERE guild_id=? AND user_id=?',
                              (guild_id, user_id)).fetchone()
        return row[0] if row else 0

    def has_player(self, guild_id, user_id):
        return self.db.execute('SELECT 1 FROM players WHERE guild_id=? AND user_id=?',
                               (guild_id, user_id)).fetchone() is not None

    def create_player(self, guild_id, user_id):
        """Create a formally invited player, returning whether it was new."""
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                return self.create_player(guild_id, user_id)
        created = self.db.execute(
            'INSERT OR IGNORE INTO players (guild_id,user_id,xp) VALUES (?,?,0)',
            (guild_id, user_id))
        return bool(created.rowcount)

    @staticmethod
    def day_key(now):
        return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()

    def daily_xp(self, guild_id, user_id, source, now=None):
        day = self.day_key(time.time() if now is None else now)
        row = self.db.execute('SELECT xp FROM rpg_daily_xp WHERE guild_id=? AND user_id=? AND day=? AND source=?',
                              (guild_id, user_id, day, source)).fetchone()
        return row[0] if row else 0

    def _daily_award(self, guild, user, source, now, amount, limit):
        if limit is None:
            return amount
        amount = max(0, min(amount, limit - self.daily_xp(guild, user, source, now)))
        if amount:
            self.db.execute('INSERT INTO rpg_daily_xp VALUES (?,?,?,?,?) '
                            'ON CONFLICT(guild_id,user_id,day,source) DO UPDATE SET xp=xp+excluded.xp',
                            (guild, user, self.day_key(now), source, amount))
        return amount

    def award_text(self, guild_id, user_id, now, amount, cooldown, daily_limit=None, *, scale=False):
        # Conditional UPSERT makes cooldown and XP one atomic, restart-safe write.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT last_text_at FROM players WHERE guild_id=? AND user_id=?',
                                  (guild_id, user_id)).fetchone()
            if row and row[0] is not None and now - row[0] < cooldown:
                return
            if scale:
                xp = self.xp(guild_id, user_id)
                amount = scaled_chat_xp(amount, xp)
                if daily_limit is not None:
                    daily_limit = scaled_chat_xp(daily_limit, xp)
            amount = self._daily_award(guild_id, user_id, 'text', now, amount, daily_limit)
            self.db.execute('''INSERT INTO players (guild_id, user_id, xp, last_text_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp=players.xp+excluded.xp, last_text_at=excluded.last_text_at
                WHERE players.last_text_at IS NULL OR ?-players.last_text_at>=?''',
                (guild_id, user_id, amount, now, now, cooldown))

    def award_voice(self, awards, daily_limit=None, now=None, *, xp_per_minute=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            now = time.time() if now is None else now
            if xp_per_minute is not None:
                # In this mode tracker amounts are complete minutes, not XP.
                scaled_awards = []
                for guild, user, minutes in awards:
                    xp = self.xp(guild, user)
                    amount = minutes * scaled_chat_xp(xp_per_minute, xp)
                    limit = None if daily_limit is None else scaled_chat_xp(daily_limit, xp)
                    scaled_awards.append((guild, user, self._daily_award(
                        guild, user, 'voice', now, amount, limit)))
                awards = scaled_awards
            else:
                awards = [(guild, user, self._daily_award(guild, user, 'voice', now, amount, daily_limit))
                          for guild, user, amount in awards]
            self.db.executemany('''INSERT INTO players (guild_id, user_id, xp)
                VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp=players.xp+excluded.xp''', awards)

    def leaders(self, guild_id):
        return self.db.execute('SELECT user_id, xp FROM players WHERE guild_id=? '
                               'ORDER BY xp DESC, user_id LIMIT 10', (guild_id,)).fetchall()


def eligible_voice_members(guild, minimum):
    result = set()
    for channel in guild.voice_channels:
        if channel == guild.afk_channel:
            continue
        members = []
        for member in channel.members:
            state = member.voice
            if not member.bot and state and not any((state.self_mute, state.mute,
                                                     state.self_deaf, state.deaf,
                                                     state.suppress)):
                members.append(member.id)
        if len(members) >= minimum:
            result.update(members)
    return result


class VoiceTracker:
    """Only continuous eligible time counts; disconnects discard pending time."""
    def __init__(self):
        self.sessions = {}

    def clear(self, guild_id=None):
        if guild_id is None:
            self.sessions.clear()
        else:
            self.sessions.pop(guild_id, None)

    def update(self, guild_id, eligible, now, xp_per_minute):
        previous = self.sessions.get(guild_id, {})
        current = {}
        awards = []
        for user_id, since in previous.items():
            minutes = max(0, int((now - since) // 60))
            if minutes:
                awards.append((guild_id, user_id, minutes * xp_per_minute))
            if user_id in eligible:
                current[user_id] = since + minutes * 60
        for user_id in eligible:
            current.setdefault(user_id, now)
        self.sessions[guild_id] = current
        return awards
