"""Durable encounter state and atomic, idempotent rewards."""
import json
import random
import uuid
from decimal import Decimal

from core.rpg_character import CharacterError

# Each monster type owns its loot table; new types default to no equipment drops.
# Entries reference the shared item catalog and may be accessories, suits or weapons.
DROP_TABLES = {
    '巨獸': tuple(f'raid:{i}' for i in range(5)),
    '毒蛛': tuple(f'raid:{i}' for i in range(5)),
    '史萊姆群': (),
    '鐵殼魔像': ('golem:hammer', 'golem:sword_shield', 'golem:bow', 'golem:staff'),
    '荊棘妖樹': ('tree:infantry', 'tree:knight', 'tree:archer', 'tree:monk'),
}


class RaidStore:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_raids (
                id TEXT PRIMARY KEY, guild_id INTEGER, channel_id INTEGER,
                status TEXT, data TEXT, delivered INTEGER NOT NULL DEFAULT 0)''')
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_raid ON rpg_raids(channel_id) WHERE status IN ('posting','lobby','running')")
            self.db.execute('CREATE TABLE IF NOT EXISTS rpg_raid_schedule (channel_id INTEGER PRIMARY KEY, next_at REAL)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_raid_difficulty (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, multiplier REAL NOT NULL,
                PRIMARY KEY (guild_id, channel_id))''')

    def difficulty(self, guild, channel):
        row = self.db.execute('SELECT multiplier FROM rpg_raid_difficulty WHERE guild_id=? AND channel_id=?',
                              (guild, channel)).fetchone()
        return row[0] if row else 1.0

    def create(self, guild, channel, monster, now, reward_policy=None, reward_overrides=None):
        if monster['kind'] == '史萊姆群':
            from dataclasses import asdict
            from core.settings import RaidSettings
            reward_policy = dict(reward_policy) if reward_policy is not None else asdict(RaidSettings())
            reward_policy['victory_xp'] *= 2
            reward_policy['victory_gold'] = reward_policy.get('victory_gold', 0) * 2
            reward_policy['drop_chance'] = 0.0
        if reward_overrides:
            reward_policy = dict(reward_policy)
            reward_policy.update(reward_overrides)
        if monster['kind'] == '史萊姆群':
            reward_policy['drop_chance'] = 0.0
        raid = dict(id=uuid.uuid4().hex, guild_id=guild, channel_id=channel, status='posting',
                    monster=monster, members=[], deadline=now + 300, message_id=None,
                    seed=random.randrange(2**31), participants=[], delivered=False, reward_policy=reward_policy,
                    drop_version=4, drop_pool=list(DROP_TABLES.get(monster['kind'], ())))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            dynamic = self.difficulty(guild, channel)
            if reward_policy is not None:
                # Freeze final rewards at announcement; explicit admin amounts remain final.
                policy = dict(reward_policy)
                scaled = []
                for key in ('victory_xp', 'victory_gold'):
                    if key in policy and key not in (reward_overrides or {}):
                        policy[key] = int(Decimal(str(dynamic)) * policy[key])
                        scaled.append(key)
                raid['reward_policy'] = policy
                raid['reward_scaling'] = dict(multiplier=dynamic, fields=scaled)
            base_strength = monster.get('strength', 1.0)
            raid['monster'] = dict(monster, strength=round(base_strength * dynamic, 6))
            raid['difficulty'] = dict(current=dynamic, base_strength=base_strength)
            self.db.execute('INSERT INTO rpg_raids(id,guild_id,channel_id,status,data) VALUES (?,?,?,?,?)',
                            (raid['id'], guild, channel, raid['status'], json.dumps(raid, ensure_ascii=False)))
        return raid

    def get(self, raid_id):
        row = self.db.execute('SELECT data FROM rpg_raids WHERE id=?', (raid_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM rpg_raids WHERE delivered=0')]

    def save(self, raid):
        with self.db:
            self._save(raid)

    def _save(self, raid):
        self.db.execute('UPDATE rpg_raids SET status=?, data=?, delivered=? WHERE id=?',
                        (raid['status'], json.dumps(raid, ensure_ascii=False), int(raid['delivered']), raid['id']))

    def next_at(self, channel):
        row = self.db.execute('SELECT next_at FROM rpg_raid_schedule WHERE channel_id=?', (channel,)).fetchone()
        return row[0] if row else None

    def schedule(self, channel, next_at):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rpg_raid_schedule VALUES (?, ?)', (channel, next_at))

    def join(self, raid_id, guild, user, now, maximum, leave=False):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            raid = self.get(raid_id)
            if not raid or raid['guild_id'] != guild or raid['status'] != 'lobby' or now >= raid['deadline']:
                raise CharacterError('報名已截止，請等待下一次討伐。')
            if leave:
                if user not in raid['members']:
                    raise CharacterError('你尚未報名。')
                raid['members'].remove(user)
            else:
                if user in raid['members']:
                    raise CharacterError('你已經報名了。')
                if len(raid['members']) >= maximum:
                    raise CharacterError('討伐隊伍已滿。')
                for other in self.pending():
                    if other['status'] in ('lobby', 'running') and user in other['members']:
                        raise CharacterError('你已參與另一場討伐，請先完成或退出。')
                raid['members'].append(user)
            self._save(raid)
            return raid

    def settle(self, raid_id, battle_data, settings):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            raid = self.get(raid_id)
            if raid['status'] == 'completed':
                return raid
            if raid['status'] != 'running' or not battle_data['result']:
                raise CharacterError('戰鬥尚未結束，不能結算。')
            victory = battle_data['result'] == '勝利'
            if raid.get('reward_policy'):
                from types import SimpleNamespace
                settings = SimpleNamespace(**raid['reward_policy'])
            rng = random.Random(raid['seed'])
            rewards = []
            for p in raid['participants']:
                xp = settings.victory_xp if victory else settings.defeat_xp
                # Older announcements had no gold reward; do not retroactively change them.
                gold = getattr(settings, 'victory_gold', 0) if victory else 0
                drop = None
                pool = raid.get('drop_pool', DROP_TABLES.get(raid['monster']['kind'], ()))
                if victory and pool and raid['monster']['kind'] != '史萊姆群' and rng.random() < settings.drop_chance:
                    drop = rng.choice(pool)
                    self.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (?,?,?) '
                                    'ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET quantity=rpg_inventory.quantity+1',
                                    (raid['guild_id'], p['id'], drop))
                self.db.execute('INSERT INTO players(guild_id,user_id,xp) VALUES (?,?,?) '
                                'ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=players.xp+excluded.xp',
                                (raid['guild_id'], p['id'], xp))
                if gold:
                    self.db.execute('INSERT INTO rpg_wallets(guild_id,user_id,gold) VALUES (?,?,?) '
                                    'ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=rpg_wallets.gold+excluded.gold',
                                    (raid['guild_id'], p['id'], gold))
                rewards.append(dict(id=p['id'], xp=xp, gold=gold, item=drop))
            raid.update(status='completed', battle=battle_data, rewards=rewards)
            if raid['participants']:
                before = self.difficulty(raid['guild_id'], raid['channel_id'])
                after = round(max(0.5, min(3.0, before * (1.1 if victory else 0.9))), 6)
                self.db.execute('INSERT INTO rpg_raid_difficulty VALUES (?,?,?) '
                                'ON CONFLICT(guild_id,channel_id) DO UPDATE SET multiplier=excluded.multiplier',
                                (raid['guild_id'], raid['channel_id'], after))
                raid['difficulty_change'] = dict(before=before, after=after)
            self._save(raid)
            return raid
