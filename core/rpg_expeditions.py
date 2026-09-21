"""Versioned legacy and alchemy-doll expeditions."""
import json
import time
import uuid

from core.rpg import level_for, record_gold
from core.rpg_character import CharacterError, add_owned_item
from core.rpg_alchemy import (BODY_MATERIAL_TIERS, STAT_NAMES, body_material_id,
                              operation_fuel_cost)
from core.rpg_monsters import TIER_VICTORY_XP


LEGACY_DURATIONS = {4: (6, 2), 8: (9, 3), 12: (12, 4)}
DURATIONS = (1, 2, 3)
EXPEDITION_ROUTES = {
    'gold': ('商路護衛', None),
    'structure': ('古代遺構', 0),
    'power': ('熔岩礦脈', 1),
    'durability': ('結晶洞窟', 2),
    'precision': ('廢棄工房', 3),
    'spirit': ('靈脈森林', 4),
}


def table_exists(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                      ('table', name)).fetchone() is not None


def _active_rows(db, user):
    if not table_exists(db, 'rpg_expeditions'):
        return []
    return db.execute("SELECT guild_id,ready_at,data FROM rpg_expeditions "
                      "WHERE user_id=? AND status='active'", (user,)).fetchall()


def is_legacy_expedition_active(db, user, now=None):
    now = time.time() if now is None else now
    return any(ready_at > now and json.loads(data).get('kind') != 'doll'
               for _guild, ready_at, data in _active_rows(db, user))


def is_doll_expedition_active(db, guild, user, now=None):
    now = time.time() if now is None else now
    return any(saved_guild == guild and ready_at > now
               and json.loads(data).get('kind') == 'doll'
               for saved_guild, ready_at, data in _active_rows(db, user))


def require_not_legacy_expedition(db, user, now=None):
    if is_legacy_expedition_active(db, user, now):
        raise CharacterError('你正在遠征，請等待返回或先中斷遠征，再參加討伐。')


def require_doll_available(db, guild, user, now=None):
    if is_doll_expedition_active(db, guild, user, now):
        raise CharacterError('煉金人偶正在遠征，返回前不能自動化或出戰。')


def require_doll_idle(db, guild, user):
    if table_exists(db, 'rpg_alchemy_operations'):
        reserved = db.execute('''SELECT 1 FROM rpg_alchemy_operations
            WHERE guild_id=? AND user_id=? AND status='reserved' LIMIT 1''',
                              (guild, user)).fetchone()
        if reserved:
            raise CharacterError('煉金人偶正在執行自動化操作，請稍後再派遣。')
    for table in ('rpg_raids', 'rpg_total_raids'):
        if not table_exists(db, table):
            continue
        for (data,) in db.execute(f"SELECT data FROM {table} WHERE guild_id=? AND status='running'",
                                  (guild,)):
            participants = json.loads(data).get('participants', ())
            if any(item.get('id') == user and item.get('doll_support')
                   for item in participants):
                raise CharacterError('煉金人偶正在戰鬥，請等待本場戰鬥結束。')


class Expeditions:
    def __init__(self, store, settings):
        self.store, self.db, self.settings = store, store.db, settings
        self.divinations = None
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_expeditions (
                id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                status TEXT NOT NULL, ready_at REAL NOT NULL, data TEXT NOT NULL)''')
            self.db.execute('DROP INDEX IF EXISTS one_pending_expedition')
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_expedition "
                            "ON rpg_expeditions(guild_id,user_id) WHERE status='active'")

    def state(self, guild, user):
        row = self.db.execute('''SELECT data FROM rpg_expeditions
            WHERE user_id=? AND status='active' AND
            (guild_id=? OR json_extract(data,'$.kind') IS NULL)
            ORDER BY CASE WHEN guild_id=? THEN 0 ELSE 1 END LIMIT 1''',
                              (user, guild, guild)).fetchone()
        return json.loads(row[0]) if row else None

    def preview(self, guild, user, hours, route='gold'):
        if hours not in DURATIONS:
            raise CharacterError('人偶遠征時間只能選擇 1、2 或 3 小時。')
        if route not in EXPEDITION_ROUTES:
            raise CharacterError('請選擇有效的人偶遠征。')
        doll = (self.db.execute('SELECT active_body,fuel FROM rpg_alchemy_dolls '
                                'WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
                if table_exists(self.db, 'rpg_alchemy_dolls') else None)
        if not doll or not doll[0]:
            raise CharacterError('請先替煉金人偶安裝素體，再派遣遠征。')
        body = json.loads(doll[0])
        level = level_for(self.store.xp(guild, user))
        player_tier = max(BODY_MATERIAL_TIERS[0], min(BODY_MATERIAL_TIERS[-1],
                          level // 10 * 10))
        material_tier = min(player_tier, body['tier'] + 10)
        route_name, stat_index = EXPEDITION_ROUTES[route]
        result = dict(kind='doll', version=2, hours=hours, route=route,
                      route_name=route_name, level=level, body_tier=body['tier'],
                      body_stats=body['stats'], fuel=operation_fuel_cost(body, hours),
                      remaining_fuel=doll[1] - operation_fuel_cost(body, hours),
                      gold=0, material=None, quantity=0)
        if stat_index is None:
            result['gold'] = sum(body['stats']) // 2 * hours
        else:
            result.update(material=body_material_id(material_tier, stat_index),
                          material_tier=material_tier, stat=STAT_NAMES[stat_index],
                          quantity=hours)
        return result

    def start(self, guild, user, hours, route='gold', now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if not self.store.has_player(guild, user):
                raise CharacterError('請先接受冒險邀請。')
            if self.state(guild, user):
                raise CharacterError('這具人偶已有遠征，請先中斷或領取完成獎勵。')
            require_doll_idle(self.db, guild, user)
            result = self.preview(guild, user, hours, route)
            fortune_status = self.divinations.status(guild, user, now) if self.divinations else {}
            fortune_card = fortune_status.get('card')
            result['fortune_card'] = fortune_card
            result['fortune_selected_at'] = fortune_status.get('selected_at')
            if fortune_card == 'hermit':
                if result['gold']:
                    result['gold'] = result['gold'] * 120 // 100
                if result.get('quantity'):
                    result['quantity'] = (result['quantity'] * 120 + 99) // 100
            paid = self.db.execute('''UPDATE rpg_alchemy_dolls SET fuel=fuel-?
                WHERE guild_id=? AND user_id=? AND fuel>=?''',
                                   (result['fuel'], guild, user, result['fuel']))
            if not paid.rowcount:
                raise CharacterError(f'鍊金燃料不足，需要 {result["fuel"]}。')
            result.update(id=uuid.uuid4().hex, guild_id=guild, user_id=user, status='active',
                          started_at=now, ready_at=now + hours * 3600)
            self.db.execute('INSERT INTO rpg_expeditions VALUES (?,?,?,?,?,?)',
                            (result['id'], guild, user, 'active', result['ready_at'],
                             json.dumps(result, ensure_ascii=False)))
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
                if result.get('kind') == 'doll':
                    if result['gold']:
                        self.db.execute('''INSERT INTO rpg_wallets VALUES (?,?,?)
                            ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold''',
                            (guild, user, result['gold']))
                        record_gold(self.db, guild, user, result['gold'],
                                    'expedition_reward', session_id, now)
                    if result.get('material'):
                        add_owned_item(self.db, guild, user,
                                       result['material'], result['quantity'])
                else:
                    self.db.execute('UPDATE players SET xp=xp+? WHERE guild_id=? AND user_id=?',
                                    (result['xp'], guild, user))
                    self.db.execute('''INSERT INTO rpg_wallets VALUES (?,?,?)
                        ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold''',
                        (guild, user, result['gold']))
                    record_gold(self.db, guild, user, result['gold'],
                                'expedition_reward', session_id, now)
                    add_owned_item(self.db, guild, user, 'proof:raid', result['proofs'])
                if self.divinations and result.get('fortune_card') == 'hermit':
                    self.divinations.resonate(guild, user, 'hermit', now=result['started_at'],
                                              activation=result.get('fortune_selected_at'),
                                              award_now=now)
            result['status'] = 'cancelled' if cancel else 'claimed'
            self.db.execute('UPDATE rpg_expeditions SET status=?,data=? WHERE id=?',
                            (result['status'], json.dumps(result, ensure_ascii=False), session_id))
            return result


__all__ = ['DURATIONS', 'EXPEDITION_ROUTES', 'LEGACY_DURATIONS', 'Expeditions',
           'is_doll_expedition_active', 'is_legacy_expedition_active',
           'require_doll_available', 'require_not_legacy_expedition']
