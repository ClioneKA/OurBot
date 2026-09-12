"""Persistent, atomic player-defined combat loadouts."""
from dataclasses import asdict
import json

from core.rpg import level_for
from core.rpg_battle import (ALLY_EFFECTS, BASIC_TARGETS, CONDITIONS, OFFENSIVE_TARGETS, PASSIVES, SKILLS,
                             TARGETS, condition_value, unlocked_skills)
from core.rpg_character import CharacterError, JOBS, item_level, stage_for


FREE_LOADOUT_SLOTS = 3
EQUIPMENT_SLOT_ORDER = ('武器', '套裝', '飾品1', '飾品2', '飾品3', '飾品4', '飾品5')


def equipment_slot_key(slot):
    try:
        return EQUIPMENT_SLOT_ORDER.index(slot)
    except ValueError:
        return len(EQUIPMENT_SLOT_ORDER)


class Loadouts:
    def __init__(self, store, characters, tactics):
        self.store, self.db = store, store.db
        self.characters, self.tactics = characters, tactics
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_loadouts (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, slot INTEGER NOT NULL,
                name TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(guild_id,user_id,slot),
                CHECK(slot >= 1))''')

    def capacity(self, guild, user):
        return self.characters.expansions.capacity(guild, user, 'expansion:loadout', FREE_LOADOUT_SLOTS)

    def _slot(self, guild, user, slot):
        if type(slot) is not int or not 1 <= slot <= self.capacity(guild, user):
            raise CharacterError('無效的出戰配置格。')
        return slot

    @staticmethod
    def _name(name):
        name = ' '.join(str(name).split()).strip()
        if not name:
            raise CharacterError('配置名稱不能空白。')
        if len(name) > 20:
            raise CharacterError('配置名稱最多 20 個字。')
        return name

    def get(self, guild, user, slot):
        slot = self._slot(guild, user, slot)
        row = self.db.execute('''SELECT name,data FROM rpg_loadouts
            WHERE guild_id=? AND user_id=? AND slot=?''', (guild, user, slot)).fetchone()
        if not row:
            return dict(slot=slot, name=f'配置 {slot}', data=None)
        try:
            data = json.loads(row[1])
        except (TypeError, json.JSONDecodeError):
            data = None
        return dict(slot=slot, name=row[0], data=data)

    def all(self, guild, user):
        return [self.get(guild, user, slot) for slot in range(1, self.capacity(guild, user) + 1)]

    def save(self, guild, user, slot):
        slot = self._slot(guild, user, slot)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.characters.snapshot(guild, user)
            if state['job'] not in JOBS:
                raise CharacterError('民兵不能保存出戰配置，請先在 Lv.10 完成轉職。')
            rules = sorted(self.tactics.rules(guild, user, state['job']), key=lambda rule: rule.slot)
            passive = self.tactics.passive(guild, user, state['job'])
            equipment = {equip_slot: state['equipped_instances'][equip_slot]
                         for equip_slot in state['slots']
                         if equip_slot in state['equipped_instances']}
            data = dict(job=state['job'], equipment=equipment,
                        rules=[asdict(rule) for rule in rules],
                        basic_target=self.tactics.basic_target(guild, user, state['job']),
                        passive_id=passive.id if passive else None)
            existing = self.db.execute('''SELECT name FROM rpg_loadouts
                WHERE guild_id=? AND user_id=? AND slot=?''', (guild, user, slot)).fetchone()
            name = existing[0] if existing else f'配置 {slot}'
            self.db.execute('''INSERT INTO rpg_loadouts(guild_id,user_id,slot,name,data)
                VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id,slot) DO UPDATE SET
                name=excluded.name,data=excluded.data''',
                (guild, user, slot, name, json.dumps(data, ensure_ascii=False)))
        return self.get(guild, user, slot)

    def rename(self, guild, user, slot, name):
        slot = self._slot(guild, user, slot)
        name = self._name(name)
        with self.db:
            self.db.execute('''INSERT INTO rpg_loadouts(guild_id,user_id,slot,name,data)
                VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id,slot) DO UPDATE SET name=excluded.name''',
                (guild, user, slot, name, 'null'))
        return self.get(guild, user, slot)

    def clear(self, guild, user, slot):
        slot = self._slot(guild, user, slot)
        with self.db:
            self.db.execute('DELETE FROM rpg_loadouts WHERE guild_id=? AND user_id=? AND slot=?',
                            (guild, user, slot))

    def _validated(self, guild, user, data):
        if not isinstance(data, dict) or data.get('job') not in JOBS:
            raise CharacterError('配置資料已損壞，請清空後重新保存。')
        job = data['job']
        level = level_for(self.store.xp(guild, user))
        if level < 10:
            raise CharacterError('目前是民兵，達到 Lv.10 才能套用出戰配置。')
        stage = stage_for(level, self.characters.settings)
        capacity = stage + 2
        valid_slots = {'武器', '套裝', *(f'飾品{i}' for i in range(1, capacity + 1))}
        equipment = data.get('equipment')
        if not isinstance(equipment, dict) or any(slot not in valid_slots for slot in equipment):
            raise CharacterError('配置含有尚未解鎖的裝備欄位。')
        validated_equipment = {}
        accessory_ids = set()
        instance_ids = set()
        for slot, raw_id in sorted(equipment.items(), key=lambda entry: equipment_slot_key(entry[0])):
            if type(raw_id) is not int or raw_id in instance_ids:
                raise CharacterError('配置中的裝備資料無效。')
            instance_ids.add(raw_id)
            instance = self.characters.get_instance(guild, user, raw_id)
            if not instance:
                continue
            item = self.characters.resolved_item(instance)
            expected = '飾品' if slot.startswith('飾品') else slot
            if item.slot != expected or item.job and item.job != job:
                raise CharacterError(f'配置中的{slot}不適合{job}。')
            if level < item_level(item, self.characters.settings):
                raise CharacterError(f'{item.name}需要 Lv.{item_level(item, self.characters.settings)}。')
            if item.slot == '飾品':
                if instance.item_id in accessory_ids:
                    raise CharacterError(f'配置不能重複穿戴同名飾品「{item.name}」。')
                accessory_ids.add(instance.item_id)
            validated_equipment[slot] = raw_id

        raw_rules = data.get('rules')
        if not isinstance(raw_rules, list) or len(raw_rules) != 3:
            raise CharacterError('配置中的技能策略資料不完整。')
        available_count = len(unlocked_skills(job, level))
        rules = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise CharacterError('配置中的技能策略資料無效。')
            required = ('slot', 'priority', 'enabled', 'condition', 'target')
            if any(key not in raw for key in required):
                raise CharacterError('配置中的技能策略資料不完整。')
            slot, priority = raw['slot'], raw['priority']
            enabled, condition, target = raw['enabled'], raw['condition'], raw['target']
            skill_id = raw.get('skill_id') or slot
            if (type(slot) is not int or slot not in (1, 2, 3) or
                    type(priority) is not int or priority not in (1, 2, 3) or
                    type(enabled) is not bool or condition not in CONDITIONS or target not in TARGETS or
                    type(skill_id) is not int or not 1 <= skill_id <= available_count):
                raise CharacterError('配置中的技能策略資料無效。')
            threshold = condition_value(condition, raw.get('condition_value'))
            skill = SKILLS[job][skill_id - 1]
            if target == 'self' and skill.effect not in ALLY_EFFECTS | {'stance', 'taunt', 'rally'}:
                raise CharacterError(f'「{skill.name}」不能以自己為目標。')
            if target == 'debuffed' and skill.effect != 'cleanse':
                raise CharacterError(f'「{skill.name}」不能使用負面狀態目標。')
            if target in OFFENSIVE_TARGETS and skill.effect in ALLY_EFFECTS:
                raise CharacterError(f'「{skill.name}」不能使用攻擊目標規則。')
            rules.append((slot, priority, enabled, condition, target,
                          None if skill_id == slot else skill_id, threshold))
        if ({rule[0] for rule in rules} != {1, 2, 3} or
                {rule[1] for rule in rules} != {1, 2, 3} or
                len({rule[5] or rule[0] for rule in rules}) != 3):
            raise CharacterError('配置中的技能槽、優先順序或技能有重複。')

        passive_id = data.get('passive_id')
        if passive_id is not None:
            valid_passives = {passive.id for passive in PASSIVES.get(job, ())} if level >= 50 else set()
            if type(passive_id) is not int or passive_id not in valid_passives:
                raise CharacterError('配置中的職業被動尚未解鎖或無效。')
        basic_target = data.get('basic_target', 'lowest')
        if basic_target not in BASIC_TARGETS:
            raise CharacterError('配置中的普通攻擊目標無效。')
        return job, validated_equipment, rules, passive_id, basic_target

    def apply(self, guild, user, slot):
        profile = self.get(guild, user, slot)
        if profile['data'] is None:
            raise CharacterError('這個配置格尚未保存內容。')
        job, equipment, rules, passive_id, basic_target = self._validated(guild, user, profile['data'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('''INSERT INTO rpg_characters(guild_id,user_id,job) VALUES (?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET job=excluded.job''', (guild, user, job))
            self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=?', (guild, user))
            self.db.executemany('INSERT INTO rpg_equipment(guild_id,user_id,slot,instance_id) VALUES (?,?,?,?)',
                                [(guild, user, equip_slot, instance_id)
                                 for equip_slot, instance_id in equipment.items()])
            self.db.execute('DELETE FROM rpg_tactics WHERE guild_id=? AND user_id=? AND job=?',
                            (guild, user, job))
            self.db.executemany('''INSERT INTO rpg_tactics
                (guild_id,user_id,job,slot,priority,enabled,condition,target,skill_id,condition_value)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                [(guild, user, job, rule_slot, priority, int(enabled), condition, target,
                  skill_id, threshold)
                 for rule_slot, priority, enabled, condition, target, skill_id, threshold in rules])
            self.db.execute('DELETE FROM rpg_passives WHERE guild_id=? AND user_id=? AND job=?',
                            (guild, user, job))
            self.db.execute('INSERT OR REPLACE INTO rpg_basic_targets VALUES (?,?,?,?)',
                            (guild, user, job, basic_target))
            if passive_id is not None:
                self.db.execute('INSERT INTO rpg_passives VALUES (?,?,?,?)',
                                (guild, user, job, passive_id))
        return self.characters.snapshot(guild, user)
