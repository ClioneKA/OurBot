"""Idempotent final rewards for Painted Maze clears."""
import json
import random
import time

from core.rpg_character import ITEMS, MAZE_CHOICE_BOXES, CharacterError, add_owned_item


JOB_KEYS = {
    '裝甲步兵': 'infantry',
    '騎士': 'knight',
    '弓兵': 'archer',
    '僧侶': 'monk',
}
JOB_BOXES = {job: f'maze:choice_box:{key}' for job, key in JOB_KEYS.items()}
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
            raise CharacterError('無效的繪境迷宮獎勵封存點。')
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT data FROM rpg_painted_maze_rooms WHERE id=?', (room_id,)).fetchone()
            if not row:
                raise CharacterError('找不到繪境迷宮房間。')
            room = json.loads(row[0])
            if not room.get('requires_entry', True):
                return []  # Administrator-created practice rooms never grant rewards.
            if room.get('reward_policy') == 'escrow_v2' and room['status'] in ('lobby', 'running', 'contract'):
                raise CharacterError('累積掉落會在離場時統一發放。')
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
                if room.get('loot_percent', 100) == 50:
                    # Round the cumulative total, not each checkpoint separately.
                    xp_bonus = 100 + fortune.get('xp_percent', 0) + tavern.get('xp_percent', 0) + meal.get('xp_percent', 0)
                    gold_bonus = 100 + meal.get('gold_percent', 0)
                    prior_xp = sum(CHECKPOINT_REWARDS[i][0] * xp_bonus // 100 for i in range(1, checkpoint))
                    prior_gold = sum(CHECKPOINT_REWARDS[i][1] * gold_bonus // 100 for i in range(1, checkpoint))
                    xp = (prior_xp + xp) // 2 - prior_xp // 2
                    gold = (prior_gold + gold) // 2 - prior_gold // 2
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

    def release_pending_boxes(self, guild_id, user_id):
        """Convert legacy unclaimed first-clear choices into inventory boxes once."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = self.db.execute('''SELECT room_id,job FROM rpg_painted_maze_final_rewards
                WHERE guild_id=? AND user_id=? AND status='pending' ORDER BY created_at''',
                (guild_id, user_id)).fetchall()
            granted = []
            for room_id, job in rows:
                box_id = JOB_BOXES.get(job)
                if not box_id:
                    continue
                add_owned_item(self.db, guild_id, user_id, box_id)
                updated = self.db.execute('''UPDATE rpg_painted_maze_final_rewards
                    SET status='box_granted',item_id=?
                    WHERE room_id=? AND guild_id=? AND user_id=? AND status='pending' ''',
                    (box_id, room_id, guild_id, user_id))
                if updated.rowcount:
                    granted.append(box_id)
            return granted

    def open_choice_box(self, guild_id, user_id, box_id, slot):
        choices = MAZE_CHOICE_BOXES.get(box_id)
        if not choices or slot not in ('weapon', 'suit'):
            raise CharacterError('這不是可使用的菁英裝備自選箱。')
        item_id = next((choice for choice in choices if choice.endswith(':' + slot)), None)
        if not item_id:
            raise CharacterError('自選箱中沒有這個裝備選項。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            consumed = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>=1''',
                (guild_id, user_id, box_id))
            if not consumed.rowcount:
                raise CharacterError('背包中沒有這個菁英裝備自選箱。')
            self.db.execute('''DELETE FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id=? AND quantity<=0''',
                (guild_id, user_id, box_id))
            equipment_id = add_owned_item(self.db, guild_id, user_id, item_id)[0]
            return item_id, equipment_id

    def seal_noah_clear(self, room_id, *, now=None):
        """Reserve first-clear choices or grant later random gear in one transaction."""
        now = int(time.time() if now is None else now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT data FROM rpg_painted_maze_rooms WHERE id=?', (room_id,)).fetchone()
            if not row:
                raise CharacterError('找不到繪境迷宮房間。')
            room = json.loads(row[0])
            if not room.get('requires_entry', True):
                return []  # Administrator-created practice rooms never grant rewards.
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
                    item_id = JOB_BOXES[job]
                    add_owned_item(self.db, room['guild_id'], user_id, item_id)
                    status, equipment_id, claimed_at = 'box_granted', None, now
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
