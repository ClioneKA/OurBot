"""Idempotent final rewards for Painted Maze clears."""
import json
import random
import time

from core.rpg_character import ITEMS, CharacterError, add_owned_item


JOB_KEYS = {
    '裝甲步兵': 'infantry',
    '騎士': 'knight',
    '弓兵': 'archer',
    '僧侶': 'monk',
}
CHECKPOINT_REWARDS = {
    1: (250, 150),
    2: (400, 250),
    3: (600, 350),
    4: (800, 500),
}


class PaintedMazeRewardStore:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_painted_maze_noah_clears (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                clear_count INTEGER NOT NULL, first_room_id TEXT NOT NULL,
                PRIMARY KEY(guild_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_painted_maze_final_rewards (
                room_id TEXT NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                route TEXT NOT NULL, job TEXT NOT NULL, status TEXT NOT NULL,
                choices TEXT NOT NULL, item_id TEXT, equipment_instance_id INTEGER,
                created_at INTEGER NOT NULL, claimed_at INTEGER,
                PRIMARY KEY(room_id,user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_painted_maze_currency_rewards (
                room_id TEXT NOT NULL, checkpoint INTEGER NOT NULL,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL, gold INTEGER NOT NULL, created_at INTEGER NOT NULL,
                PRIMARY KEY(room_id,checkpoint,user_id))''')

    def seal_currency(self, room_id, checkpoint, *, now=None):
        if checkpoint not in CHECKPOINT_REWARDS:
            raise CharacterError('無效的繪境迷廊獎勵封存點。')
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT data FROM rpg_painted_maze_rooms WHERE id=?', (room_id,)).fetchone()
            if not row:
                raise CharacterError('找不到繪境迷廊房間。')
            room = json.loads(row[0])
            if checkpoint <= 3 and room.get('stage', 0) < checkpoint:
                raise CharacterError('這個階段尚未完成。')
            if checkpoint == 4 and room.get('status') != 'completed':
                raise CharacterError('尾王尚未擊敗。')
            base_xp, base_gold = CHECKPOINT_REWARDS[checkpoint]
            result = []
            for participant in room['participants']:
                user_id = participant['id']
                existing = self.db.execute('''SELECT xp,gold FROM rpg_painted_maze_currency_rewards
                    WHERE room_id=? AND checkpoint=? AND user_id=?''',
                    (room_id, checkpoint, user_id)).fetchone()
                if existing:
                    result.append(dict(user_id=user_id, xp=existing[0], gold=existing[1]))
                    continue
                fortune = participant.get('fortune') or {}
                tavern = participant.get('tavern') or {}
                meal = participant.get('meal') or {}
                xp = base_xp * (100 + fortune.get('xp_percent', 0)
                                + tavern.get('xp_percent', 0)
                                + meal.get('xp_percent', 0)) // 100
                gold = base_gold * (100 + meal.get('gold_percent', 0)) // 100
                self.db.execute('''INSERT INTO rpg_painted_maze_currency_rewards
                    VALUES (?,?,?,?,?,?,?)''',
                    (room_id, checkpoint, room['guild_id'], user_id, xp, gold, now))
                self.db.execute('''INSERT INTO players(guild_id,user_id,xp) VALUES (?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=players.xp+excluded.xp''',
                    (room['guild_id'], user_id, xp))
                self.db.execute('''INSERT INTO rpg_wallets(guild_id,user_id,gold) VALUES (?,?,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=rpg_wallets.gold+excluded.gold''',
                    (room['guild_id'], user_id, gold))
                result.append(dict(user_id=user_id, xp=xp, gold=gold))
            return result

    def rewards(self, room_id):
        rows = self.db.execute('''SELECT user_id,route,job,status,choices,item_id,
            equipment_instance_id,created_at,claimed_at
            FROM rpg_painted_maze_final_rewards WHERE room_id=? ORDER BY user_id''',
            (room_id,)).fetchall()
        return [{
            'room_id': room_id, 'user_id': row[0], 'route': row[1], 'job': row[2], 'status': row[3],
            'choices': json.loads(row[4]), 'item_id': row[5],
            'equipment_instance_id': row[6], 'created_at': row[7], 'claimed_at': row[8],
        } for row in rows]

    def currency_rewards(self, room_id, checkpoint=None):
        query = '''SELECT checkpoint,user_id,xp,gold
            FROM rpg_painted_maze_currency_rewards WHERE room_id=?'''
        args = [room_id]
        if checkpoint is not None:
            query += ' AND checkpoint=?'
            args.append(checkpoint)
        query += ' ORDER BY checkpoint,user_id'
        return [dict(checkpoint=row[0], user_id=row[1], xp=row[2], gold=row[3])
                for row in self.db.execute(query, args).fetchall()]

    def latest_pending(self, guild_id, user_id):
        row = self.db.execute('''SELECT room_id FROM rpg_painted_maze_final_rewards
            WHERE guild_id=? AND user_id=? AND status='pending'
            ORDER BY created_at DESC LIMIT 1''', (guild_id, user_id)).fetchone()
        if not row:
            return None
        return next(reward for reward in self.rewards(row[0]) if reward['user_id'] == user_id)

    def seal_noah_clear(self, room_id, *, now=None):
        """Reserve first-clear choices or grant later random gear in one transaction."""
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT data FROM rpg_painted_maze_rooms WHERE id=?', (room_id,)).fetchone()
            if not row:
                raise CharacterError('找不到繪境迷廊房間。')
            room = json.loads(row[0])
            if room.get('route') != 'noah' or room.get('status') != 'completed':
                raise CharacterError('尚未擊敗繪畫魔女，不能封存菁英裝備。')
            jobs = {participant['id']: participant['state']['job']
                    for participant in room['participants']}
            for user_id in room['members']:
                existing = self.db.execute('''SELECT 1 FROM rpg_painted_maze_final_rewards
                    WHERE room_id=? AND user_id=?''', (room_id, user_id)).fetchone()
                if existing:
                    continue
                job = jobs.get(user_id)
                job_key = JOB_KEYS.get(job)
                if not job_key:
                    raise CharacterError('參戰快照中的職業無法對應菁英裝備。')
                choices = [f'maze:{job_key}:weapon', f'maze:{job_key}:suit']
                clear = self.db.execute('''SELECT clear_count FROM rpg_painted_maze_noah_clears
                    WHERE guild_id=? AND user_id=?''',
                    (room['guild_id'], user_id)).fetchone()
                if clear is None:
                    self.db.execute('''INSERT INTO rpg_painted_maze_noah_clears
                        VALUES (?,?,1,?)''', (room['guild_id'], user_id, room_id))
                    status, item_id, equipment_id, claimed_at = 'pending', None, None, None
                else:
                    self.db.execute('''UPDATE rpg_painted_maze_noah_clears
                        SET clear_count=clear_count+1 WHERE guild_id=? AND user_id=?''',
                        (room['guild_id'], user_id))
                    rng = random.Random(f'{room["seed"]}:noah-gear:{user_id}')
                    item_id = rng.choice(choices)
                    equipment_id = add_owned_item(
                        self.db, room['guild_id'], user_id, item_id)[0]
                    status, claimed_at = 'granted', now
                self.db.execute('''INSERT INTO rpg_painted_maze_final_rewards
                    (room_id,guild_id,user_id,route,job,status,choices,item_id,
                     equipment_instance_id,created_at,claimed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (room_id, room['guild_id'], user_id, 'noah', job, status,
                     json.dumps(choices), item_id, equipment_id, now, claimed_at))
            return self.rewards(room_id)

    def choose_noah_gear(self, room_id, guild_id, user_id, item_id, *, now=None):
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT status,choices,item_id,equipment_instance_id
                FROM rpg_painted_maze_final_rewards
                WHERE room_id=? AND guild_id=? AND user_id=?''',
                (room_id, guild_id, user_id)).fetchone()
            if not row:
                raise CharacterError('找不到你的諾亞通關獎勵。')
            if row[0] == 'granted':
                if row[2] != item_id:
                    raise CharacterError('這份通關獎勵已領取。')
                return row[3]
            choices = json.loads(row[1])
            if row[0] != 'pending' or item_id not in choices or item_id not in ITEMS:
                raise CharacterError('只能從首次通關提供的裝備中選擇。')
            equipment_id = add_owned_item(self.db, guild_id, user_id, item_id)[0]
            updated = self.db.execute('''UPDATE rpg_painted_maze_final_rewards
                SET status='granted',item_id=?,equipment_instance_id=?,claimed_at=?
                WHERE room_id=? AND guild_id=? AND user_id=? AND status='pending' ''',
                (item_id, equipment_id, now, room_id, guild_id, user_id))
            if not updated.rowcount:
                raise CharacterError('通關獎勵狀態已改變，請重新整理。')
            return equipment_id
