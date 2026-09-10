"""Hanna's friendship and shared tailoring prices."""


def initialize_affinity(db):
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS rpg_hanna_affinity (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            score INTEGER NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
            PRIMARY KEY(guild_id,user_id))''')


def hanna_affinity(db, guild, user):
    row = db.execute('SELECT score FROM rpg_hanna_affinity WHERE guild_id=? AND user_id=?',
                     (guild, user)).fetchone()
    return row[0] if row else 0


def tailoring_price(db, guild, user, base):
    # Each point discounts 0.25%; round the final charge up to whole gold.
    score = max(0, min(100, hanna_affinity(db, guild, user)))
    return (base * (400 - score) + 399) // 400
