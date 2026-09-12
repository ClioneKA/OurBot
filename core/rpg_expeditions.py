"""Offline expeditions with frozen rewards and atomic, restart-safe settlement."""
import json
import time
import uuid

from core.rpg import level_for, record_gold
from core.rpg_character import CharacterError, add_owned_item
from core.rpg_monsters import TIER_VICTORY_XP


# Reward numerator over eight; proof rewards do not scale with level.
DURATIONS = {4: (6, 2), 8: (9, 3), 12: (12, 4)}


def table_exists(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                      ('table', name)).fetchone() is not None


def is_expedition_active(db, user, now=None):
    if not table_exists(db, 'rpg_expeditions'):
        return False
    return db.execute("SELECT 1 FROM rpg_expeditions WHERE user_id=? "
                      "AND status='active' AND ready_at>?",
                      (user, time.time() if now is None else now)).fetchone() is not None


def require_not_expedition(db, user, now=None):
    if is_expedition_active(db, user, now):
        raise CharacterError('你正在遠征，請等待返回或先中斷遠征，再參加討伐。')


def require_no_battle(db, user):
    for table in ('rpg_raids', 'rpg_total_raids', 'rpg_painted_maze_rooms'):
        if not table_exists(db, table):
            continue
        for (data,) in db.execute(f"SELECT data FROM {table} WHERE status IN "
                                  "('posting','lobby','running','contract')"):
            if user in json.loads(data).get('members', []):
                raise CharacterError('你已報名或正在參加討伐／迷宮，請先退出或完成後再遠征。')


class Expeditions:
    def __init__(self, store, settings):
        self.store, self.db, self.settings = store, store.db, settings
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_expeditions (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                status TEXT NOT NULL, ready_at REAL NOT NULL, data TEXT NOT NULL)''')
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_expedition "
                            "ON rpg_expeditions(user_id) WHERE status='active'")

    def state(self, user):
        row = self.db.execute("SELECT data FROM rpg_expeditions WHERE user_id=? AND status='active'",
                              (user,)).fetchone()
        return json.loads(row[0]) if row else None

    def preview(self, guild, user, hours):
        if hours not in DURATIONS:
            raise CharacterError('遠征時間只能選擇 4、8 或 12 小時。')
        level = level_for(self.store.xp(guild, user))
        tier = min(6, max(1, level // 10))
        pool = 'high_raid' if tier >= 5 else 'mid_raid' if tier >= 3 else 'raid'
        numerator, proofs = DURATIONS[hours]
        return dict(hours=hours, level=level, tier=tier, proofs=proofs,
                    xp=TIER_VICTORY_XP[tier] * numerator // 8,
                    gold=getattr(self.settings, pool).victory_gold * numerator // 8)

    def start(self, guild, user, hours, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if not self.store.has_player(guild, user):
                raise CharacterError('請先接受冒險邀請。')
            if self.state(user):
                raise CharacterError('你已有遠征，請先中斷或領取完成獎勵。')
            require_no_battle(self.db, user)
            result = self.preview(guild, user, hours)
            result.update(id=uuid.uuid4().hex, guild_id=guild, user_id=user, status='active',
                          started_at=now, ready_at=now + hours * 3600)
            self.db.execute('INSERT INTO rpg_expeditions VALUES (?,?,?,?,?,?)',
                            (result['id'], guild, user, 'active', result['ready_at'],
                             json.dumps(result)))
            return result

    def finish(self, guild, user, session_id, *, cancel=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT data FROM rpg_expeditions WHERE id=? AND guild_id=? AND user_id=?',
                                  (session_id, guild, user)).fetchone()
            if not row:
                raise CharacterError('找不到這趟遠征，請回到出發的伺服器操作。')
            result = json.loads(row[0])
            if result['status'] != 'active':
                raise CharacterError('這趟遠征已處理，請重新整理。')
            if cancel and now >= result['ready_at']:
                raise CharacterError('遠征已完成，請直接領取獎勵。')
            if not cancel and now < result['ready_at']:
                raise CharacterError('遠征尚未完成。')
            if not cancel:
                self.db.execute('UPDATE players SET xp=xp+? WHERE guild_id=? AND user_id=?',
                                (result['xp'], guild, user))
                self.db.execute('''INSERT INTO rpg_wallets VALUES (?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold''',
                    (guild, user, result['gold']))
                record_gold(self.db, guild, user, result['gold'], 'expedition_reward', session_id, now)
                add_owned_item(self.db, guild, user, 'proof:raid', result['proofs'])
            result['status'] = 'cancelled' if cancel else 'claimed'
            self.db.execute('UPDATE rpg_expeditions SET status=?,data=? WHERE id=?',
                            (result['status'], json.dumps(result), session_id))
            return result
