"""Daily tavern commissions; all rewards and deliveries are atomic."""
from datetime import datetime, timedelta, timezone
import json
import random
import time
import sqlite3

from core.rpg_character import CharacterError
from core.rpg_monsters import PROFILES
from core.rpg_provisions import INGREDIENTS
from core.rpg_affinity import initialize_affinity, hanna_affinity


TAIPEI = timezone(timedelta(hours=8))
FOOD_TARGETS = tuple(key for key, ingredient in INGREDIENTS.items()
                     if not ingredient.seasoning and 1 <= ingredient.quality <= 5
                     and not key.startswith('fishing:boss:'))


def commission_day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, TAIPEI).date().isoformat()


class DailyCommissions:
    def __init__(self, store, memory=None):
        self.store, self.db = store, store.db
        self.memory = memory
        initialize_affinity(self.db)
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_daily_commission_boards (
                guild_id INTEGER NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (guild_id,day))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_daily_commission_kills (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
                kind TEXT NOT NULL, quantity INTEGER NOT NULL,
                PRIMARY KEY (guild_id,user_id,day,kind))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_daily_commission_claims (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
                npc TEXT NOT NULL, PRIMARY KEY (guild_id,user_id,day,npc))''')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_daily_commission_claims)')}
            if 'delivered' not in columns:
                # Old gold claims remain completed; do not grant a second reward.
                self.db.execute('ALTER TABLE rpg_daily_commission_claims ADD COLUMN delivered INTEGER NOT NULL DEFAULT 1')

    def _board(self, guild, day):
        row = self.db.execute('SELECT data FROM rpg_daily_commission_boards WHERE guild_id=? AND day=?',
                              (guild, day)).fetchone()
        if row:
            board = json.loads(row[0])
            for npc, quest in board.items():
                quest.pop('gold', None)
                quest['affinity'] = 1 if npc == 'annan' else INGREDIENTS[quest['target']].quality
            return board
        rng = random.Random(f'tavern-commissions:{guild}:{day}')
        targets = tuple(kind for kind, profile in PROFILES.items() if profile[0] <= 2)
        food = rng.choice(FOOD_TARGETS)
        board = dict(annan=dict(target=rng.choice(targets), quantity=1, affinity=1),
                     hanna=dict(target=food, quantity=rng.randint(2, 4), affinity=INGREDIENTS[food].quality))
        self.db.execute('INSERT INTO rpg_daily_commission_boards VALUES (?,?,?)',
                        (guild, day, json.dumps(board, ensure_ascii=False)))
        return board

    def _progress(self, guild, user, day, npc, quest):
        if npc == 'annan':
            row = self.db.execute('''SELECT quantity FROM rpg_daily_commission_kills
                WHERE guild_id=? AND user_id=? AND day=? AND kind=?''',
                (guild, user, day, quest['target'])).fetchone()
        else:
            row = self.db.execute('''SELECT quantity FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id=?''', (guild, user, quest['target'])).fetchone()
        return row[0] if row else 0

    def board(self, guild, user, now=None):
        try:
            self.deliver_pending(guild, user)
        except CharacterError:
            pass  # Keep the tavern usable while the durable reward awaits retry.
        day = commission_day(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            board = self._board(guild, day)
            for npc, quest in board.items():
                quest['progress'] = self._progress(guild, user, day, npc, quest)
                quest['claimed'] = bool(self.db.execute('''SELECT 1 FROM rpg_daily_commission_claims
                    WHERE guild_id=? AND user_id=? AND day=? AND npc=?''',
                    (guild, user, day, npc)).fetchone())
                quest['pending'] = bool(self.db.execute('''SELECT 1 FROM rpg_daily_commission_claims
                    WHERE guild_id=? AND user_id=? AND day=? AND npc=? AND delivered=0''',
                    (guild, user, day, npc)).fetchone())
        return day, board

    def deliver_pending(self, guild, user):
        rows = self.db.execute('''SELECT day FROM rpg_daily_commission_claims
            WHERE guild_id=? AND user_id=? AND npc='annan' AND delivered=0 ORDER BY day''',
            (guild, user)).fetchall()
        result = None
        for (day,) in rows:
            if self.memory is None:
                raise CharacterError('安安好感度暫時無法連線，委託獎勵已保留，請稍後再試。')
            try:
                result = self.memory.award_commission_affinity(guild, user, day)
                with self.db:
                    self.db.execute('''UPDATE rpg_daily_commission_claims SET delivered=1
                        WHERE guild_id=? AND user_id=? AND day=? AND npc='annan' ''', (guild, user, day))
            except sqlite3.Error as exc:
                raise CharacterError('好感度發放暫時失敗，獎勵已保留，重新開啟酒館即可補發。') from exc
        return result

    def record_victory(self, raid, now=None):
        """Called inside the raid's idempotent settlement transaction."""
        if raid.get('source') == 'admin':
            return
        day = commission_day(now)
        for user in {p['id'] for p in raid['participants']}:
            self.db.execute('''INSERT INTO rpg_daily_commission_kills VALUES (?,?,?,?,1)
                ON CONFLICT(guild_id,user_id,day,kind) DO UPDATE SET quantity=quantity+1''',
                (raid['guild_id'], user, day, raid['monster']['kind']))

    def claim(self, guild, user, day, npc, now=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if day != commission_day(now):
                raise CharacterError('每日委託已更新，請重新整理後再交付。')
            if not self.store.has_player(guild, user):
                raise CharacterError('請先接受邀請函，正式成為冒險者。')
            board = self._board(guild, day)
            if npc not in board:
                raise CharacterError('找不到這張委託。')
            if self.db.execute('''SELECT 1 FROM rpg_daily_commission_claims
                    WHERE guild_id=? AND user_id=? AND day=? AND npc=?''',
                    (guild, user, day, npc)).fetchone():
                raise CharacterError('今天已經完成這張委託了，明天再來吧！')
            quest = board[npc]
            if self._progress(guild, user, day, npc, quest) < quest['quantity']:
                raise CharacterError('尚未達成討伐目標。' if npc == 'annan' else '背包中的指定食材不足。')
            if npc == 'hanna':
                self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                    WHERE guild_id=? AND user_id=? AND item_id=?''',
                    (quest['quantity'], guild, user, quest['target']))
            if npc == 'annan' and self.memory is None:
                raise CharacterError('安安好感度暫時無法連線，請稍後再領取。')
            self.db.execute('''INSERT INTO rpg_daily_commission_claims
                (guild_id,user_id,day,npc,delivered) VALUES (?,?,?,?,?)''',
                (guild, user, day, npc, int(npc == 'hanna')))
            if npc == 'hanna':
                before = hanna_affinity(self.db, guild, user)
                score = min(100, before + quest['affinity'])
                self.db.execute('''INSERT INTO rpg_hanna_affinity VALUES (?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET score=excluded.score''', (guild, user, score))
                result = dict(score=score, delta=score - before)
        return self.deliver_pending(guild, user) if npc == 'annan' else result
