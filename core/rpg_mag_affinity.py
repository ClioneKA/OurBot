"""Mag intimacy progression and alchemy-doll fuel capacity rewards."""
import time


BASE_FUEL_CAPACITY = 1000
DAILY_AFFINITY_CAP = 3
FUEL_MILESTONES = (
    (0, 1000, '原始燃料槽'),
    (25, 1250, '刻痕燃料槽'),
    (50, 1500, '星盤燃料槽'),
    (75, 1750, '帷幕燃料槽'),
    (100, 2000, '命運燃料槽'),
)


def initialize_mag_affinity(db):
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS rpg_mag_affinity (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            score INTEGER NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
            PRIMARY KEY(guild_id,user_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS rpg_mag_affinity_daily (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0 CHECK(points BETWEEN 0 AND 3),
            selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
            PRIMARY KEY(guild_id,user_id,day))''')


def mag_affinity(db, guild, user):
    row = db.execute('SELECT score FROM rpg_mag_affinity WHERE guild_id=? AND user_id=?',
                     (guild, user)).fetchone()
    return row[0] if row else 0


def fuel_capacity(db, guild, user):
    score = mag_affinity(db, guild, user)
    return max(capacity for required, capacity, _name in FUEL_MILESTONES
               if score >= required)


def affinity_reward(score):
    return max((required, capacity, name) for required, capacity, name in FUEL_MILESTONES
               if score >= required)


def affinity_status(db, store, guild, user, now=None):
    now = time.time() if now is None else now
    day = store.day_key(now)
    score = mag_affinity(db, guild, user)
    row = db.execute('''SELECT points,selected FROM rpg_mag_affinity_daily
        WHERE guild_id=? AND user_id=? AND day=?''', (guild, user, day)).fetchone()
    required, capacity, name = affinity_reward(score)
    next_reward = next(((need, cap, label) for need, cap, label in FUEL_MILESTONES
                        if need > score), None)
    return dict(score=score, today=row[0] if row else 0,
                selected=bool(row[1]) if row else False,
                fuel_capacity=capacity, reward_name=name, next_reward=next_reward)


def _award(db, store, guild, user, now, *, selection):
    now = time.time() if now is None else now
    day = store.day_key(now)
    db.execute('''INSERT OR IGNORE INTO rpg_mag_affinity_daily
        (guild_id,user_id,day) VALUES (?,?,?)''', (guild, user, day))
    points, selected = db.execute('''SELECT points,selected FROM rpg_mag_affinity_daily
        WHERE guild_id=? AND user_id=? AND day=?''', (guild, user, day)).fetchone()
    score = mag_affinity(db, guild, user)
    if points >= DAILY_AFFINITY_CAP or score >= 100 or (selection and selected):
        return dict(score=score, today=points, delta=0)
    db.execute('''UPDATE rpg_mag_affinity_daily SET points=points+1,
        selected=CASE WHEN ? THEN 1 ELSE selected END
        WHERE guild_id=? AND user_id=? AND day=?''',
        (int(selection), guild, user, day))
    db.execute('''INSERT INTO rpg_mag_affinity(guild_id,user_id,score) VALUES (?,?,1)
        ON CONFLICT(guild_id,user_id) DO UPDATE SET score=MIN(100,score+1)''',
        (guild, user))
    return dict(score=min(100, score + 1), today=points + 1, delta=1)


def award_selection(db, store, guild, user, now=None):
    return _award(db, store, guild, user, now, selection=True)


def award_resonance(db, store, guild, user, now=None):
    return _award(db, store, guild, user, now, selection=False)
