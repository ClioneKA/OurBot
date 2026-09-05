"""Cooking, alchemy, persistent loadouts, and exactly-once raid consumption."""
import json

from core.rpg_character import CharacterError, ITEMS


FOODS = {
    'food:pond:common': dict(ingredients=('fishing:pond:common', 'farming:potato'),
                             heal_permille=150, regen_permille=0, regen_rounds=0),
    'food:pond:rare': dict(ingredients=('fishing:pond:rare', 'farming:wheat'),
                           heal_permille=150, regen_permille=50, regen_rounds=2),
    'food:lake:common': dict(ingredients=('fishing:lake:common', 'farming:witch_tomato'),
                             heal_permille=250, regen_permille=0, regen_rounds=0),
    'food:lake:rare': dict(ingredients=('fishing:lake:rare', 'farming:chili'),
                           heal_permille=250, regen_permille=75, regen_rounds=2),
}

POTION_KINDS = {
    'hp': ('HP', 'percent'), 'attack': ('攻擊', 'percent'),
    'defense': ('防禦', 'percent'), 'healing': ('治療量', 'percent'),
    'hit': ('命中率', 'points'), 'evasion': ('閃避率', 'points'),
    'critical': ('暴擊率', 'points'),
}

POTIONS = {}
for tier, ingredients, percent, points in (
    (1, ('fishing:pond:weed', 'farming:dew_herb'), 5,
     {'hit': 3, 'evasion': 2, 'critical': 3}),
    (2, ('fishing:lake:weed', 'farming:moonbell'), 8,
     {'hit': 5, 'evasion': 3, 'critical': 5}),
):
    for kind, (stat, mode) in POTION_KINDS.items():
        POTIONS[f'potion:{tier}:{kind}'] = dict(
            ingredients=ingredients, stat=stat, mode=mode,
            amount=points[kind] if mode == 'points' else percent)

RECIPES = {**FOODS, **POTIONS}


class Provisions:
    def __init__(self, store):
        self.store, self.db = store, store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_provision_loadouts (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                item_id TEXT NOT NULL, PRIMARY KEY(guild_id,user_id,kind))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_provision_uses (
                raid_id TEXT NOT NULL, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(raid_id,user_id))''')

    def craft(self, guild, user, item_id):
        recipe = RECIPES.get(item_id)
        if not recipe:
            raise CharacterError('請重新選擇料理或藥水。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            counts = dict(self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
                WHERE guild_id=? AND user_id=?''', (guild, user)))
            missing = [ITEMS[key].name for key in recipe['ingredients'] if counts.get(key, 0) < 1]
            if missing:
                raise CharacterError('缺少材料：' + '、'.join(missing))
            for key in recipe['ingredients']:
                self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                    WHERE guild_id=? AND user_id=? AND item_id=?''', (guild, user, key))
                self.db.execute('''DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=?
                    AND item_id=? AND quantity=0''', (guild, user, key))
            self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (?,?,?,1) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+1''', (guild, user, item_id))
        return ITEMS[item_id]

    def loadout(self, guild, user):
        selected = dict(self.db.execute('''SELECT kind,item_id FROM rpg_provision_loadouts
            WHERE guild_id=? AND user_id=?''', (guild, user)))
        counts = dict(self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
            WHERE guild_id=? AND user_id=?''', (guild, user)))
        return {kind: key for kind, key in selected.items() if counts.get(key, 0) > 0}

    def select(self, guild, user, kind, item_id=None):
        catalog = FOODS if kind == 'food' else POTIONS if kind == 'potion' else None
        if catalog is None or item_id is not None and item_id not in catalog:
            raise CharacterError('請重新選擇料理或藥水。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if item_id is None:
                self.db.execute('''DELETE FROM rpg_provision_loadouts
                    WHERE guild_id=? AND user_id=? AND kind=?''', (guild, user, kind))
                return None
            if not self.db.execute('''SELECT 1 FROM rpg_inventory
                    WHERE guild_id=? AND user_id=? AND item_id=? AND quantity>0''',
                    (guild, user, item_id)).fetchone():
                raise CharacterError('背包中沒有這項補給。')
            self.db.execute('''INSERT INTO rpg_provision_loadouts VALUES (?,?,?,?)
                ON CONFLICT(guild_id,user_id,kind) DO UPDATE SET item_id=excluded.item_id''',
                (guild, user, kind, item_id))
        return ITEMS[item_id]

    def prepare_for_raid(self, raid_id, guild, users):
        """Consume selected items once and return frozen effects for each user."""
        result = {}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for user in users:
                saved = self.db.execute('''SELECT data FROM rpg_provision_uses
                    WHERE raid_id=? AND user_id=?''', (raid_id, user)).fetchone()
                if saved:
                    result[user] = json.loads(saved[0])
                    continue
                selected = dict(self.db.execute('''SELECT kind,item_id FROM rpg_provision_loadouts
                    WHERE guild_id=? AND user_id=?''', (guild, user)))
                counts = dict(self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
                    WHERE guild_id=? AND user_id=?''', (guild, user)))
                data = {}
                for kind, catalog in (('food', FOODS), ('potion', POTIONS)):
                    key = selected.get(kind)
                    if key not in catalog or counts.get(key, 0) < 1:
                        continue
                    effect = {field: value for field, value in catalog[key].items() if field != 'ingredients'}
                    data[kind] = dict(item_id=key, name=ITEMS[key].name, **effect)
                    counts[key] -= 1
                    self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-1
                        WHERE guild_id=? AND user_id=? AND item_id=?''', (guild, user, key))
                    self.db.execute('''DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=?
                        AND item_id=? AND quantity=0''', (guild, user, key))
                    if counts[key] == 0:
                        self.db.execute('''DELETE FROM rpg_provision_loadouts
                            WHERE guild_id=? AND user_id=? AND kind=?''', (guild, user, kind))
                payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
                self.db.execute('INSERT INTO rpg_provision_uses VALUES (?,?,?,?)',
                                (raid_id, guild, user, payload))
                result[user] = data
        return result
