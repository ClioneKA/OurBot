"""Persistent server-local XP and voice participation accounting."""
from bisect import bisect_right
from math import floor
from pathlib import Path
import sqlite3


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
        self.db.commit()

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

    def award_text(self, guild_id, user_id, now, amount, cooldown):
        # Conditional UPSERT makes cooldown and XP one atomic, restart-safe write.
        with self.db:
            self.db.execute('''INSERT INTO players (guild_id, user_id, xp, last_text_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp=players.xp+excluded.xp, last_text_at=excluded.last_text_at
                WHERE players.last_text_at IS NULL OR ?-players.last_text_at>=?''',
                (guild_id, user_id, amount, now, now, cooldown))

    def award_voice(self, awards):
        with self.db:
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
