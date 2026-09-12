"""Rolled pigment-crystal instances and idempotent Painted Maze stage rewards."""
from dataclasses import dataclass
import json
import random
import time

from core.rpg_character import CharacterError, ITEMS
from core.rpg import record_gold


CRYSTAL_TYPES = {
    'outline': '輪廓顏料結晶',
    'color': '色彩顏料結晶',
    'source': '源色顏料結晶',
}
STAGE_CRYSTAL_TYPES = {1: 'outline', 2: 'color', 3: 'source'}
QUALITY_WEIGHTS = {'習作': 70, '精製': 25, '傑作': 5}
QUALITY_MULTIPLIERS = {'習作': 1.0, '精製': 1.3, '傑作': 1.65}
QUALITY_SELL_PRICES = {'習作': 600, '精製': 1200, '傑作': 2500}
CRYSTAL_REMOVAL_PRICE = 1500

# label, effect key, base minimum, base maximum.  Composite outline affixes
# split one budget between two values instead of granting two full affixes.
OUTLINE_AFFIXES = {
    'outline_hp': ('生命輪廓', ('HP',), (90,), (130,)),
    'outline_attack': ('力量輪廓', ('攻擊',), (12,), (18,)),
    'outline_defense': ('守備輪廓', ('防禦',), (12,), (18,)),
    'outline_healing': ('祈癒輪廓', ('治療量',), (12,), (18,)),
    'outline_hp_defense': ('壁壘輪廓', ('HP', '防禦'), (50, 6), (75, 9)),
    'outline_attack_hp': ('猛進輪廓', ('攻擊', 'HP'), (6, 45), (9, 65)),
    'outline_attack_healing': ('輝刃輪廓', ('攻擊', '治療量'), (6, 6), (9, 9)),
}

COLOR_AFFIXES = {
    'color_poison_damage': ('毒彩追擊', 'poison_damage_percent', 5, 8),
    'color_break_damage': ('裂彩追擊', 'break_damage_percent', 5, 8),
    'color_execute': ('殘像收束', 'low_enemy_damage_percent', 5, 9),
    'color_healthy': ('鮮明構圖', 'high_hp_damage_percent', 4, 7),
    'color_fatal_guard': ('留白保命', 'survive_fatal_once', 1, 1),
    'color_opening_shield': ('底色護幕', 'opening_shield_percent', 4, 7),
    'color_low_guard': ('暗部防護', 'low_hp_reduction_percent', 5, 9),
    'color_kill_heal': ('終筆回生', 'kill_heal_percent', 3, 6),
    'color_cleanse_heal': ('洗彩療癒', 'cleanse_heal_percent', 3, 6),
    'color_accuracy': ('精準色點', 'accuracy', 4, 8),
    'color_critical': ('銳利色點', 'critical_points', 1, 3),
    'color_evasion': ('流動色點', 'evasion_points', 1, 3),
    'color_speed': ('疾速色點', 'speed', 3, 6),
    'color_lifesteal': ('血色回流', 'lifesteal_percent', 1, 3),
    'color_healing_received': ('柔光承接', 'healing_received_percent', 3, 6),
}

SOURCE_AFFIXES = {
    '裝甲步兵': {
        'source_tempered_embers': ('百鍊餘火', 'tempered_embers', 3, 5),
        'source_formation_breaker': ('破陣重擊', 'formation_breaker', 3, 5),
        'source_blooded_blade': ('浴血鋒刃', 'blooded_blade', 4, 7),
        'source_endless_offense': ('攻守不息', 'endless_offense', 5, 8),
        'source_heavy_suppression': ('重甲壓制', 'heavy_suppression', 4, 7),
    },
    '騎士': {
        'source_vengeance_mark': ('復仇刻痕', 'vengeance_mark', 4, 7),
        'source_guardian_oath': ('守護誓印', 'guardian_oath', 4, 7),
        'source_immovable_wall': ('不動城牆', 'immovable_wall', 3, 5),
        'source_steel_echo': ('鋼鐵反響', 'steel_echo', 4, 7),
        'source_life_lance': ('生命槍鋒', 'life_lance', 4, 7),
    },
    '弓兵': {
        'source_endless_arrow': ('無盡箭痕', 'endless_arrow', 2, 4),
        'source_focused_shot': ('集中射擊', 'focused_shot', 2, 4),
        'source_venom_amplifier': ('猛毒增幅', 'venom_amplifier', 3, 5),
        'source_hunting_rhythm': ('狩獵節奏', 'hunting_rhythm', 3, 5),
        'source_heart_calibration': ('穿心校準', 'heart_calibration', 1, 3),
    },
    '僧侶': {
        'source_grace_reserve': ('恩典積累', 'grace_reserve', 4, 7),
        'source_holy_afterglow': ('聖光餘韻', 'holy_afterglow', 4, 7),
        'source_suffering_prayer': ('苦難祈禱', 'suffering_prayer', 5, 8),
        'source_pure_faith': ('潔淨信仰', 'pure_faith', 4, 7),
        'source_threefold_cast': ('三重詠唱', 'threefold_cast', 4, 7),
    },
}


def crystal_affix_name(crystal):
    if crystal.crystal_type == 'outline':
        return OUTLINE_AFFIXES[crystal.affix_id][0]
    if crystal.crystal_type == 'color':
        return COLOR_AFFIXES[crystal.affix_id][0]
    return SOURCE_AFFIXES[crystal.job][crystal.affix_id][0]


def crystal_effect_text(crystal):
    if crystal.crystal_type == 'source':
        value = crystal.rolled_values[0]
        templates = {
            'tempered_embers': '連續使用不同傷害技能時疊層（最多 5），每層傷害 +{v}%；重複技能失去 2 層',
            'formation_breaker': '攻擊破防目標時疊層（最多 5），每層傷害 +{v}%',
            'blooded_blade': '每回合首次受直接傷害疊 1 層（最多 5）；下次傷害行動每層 +{v}% 並消耗',
            'endless_offense': '使用準備技能後，下一次傷害行動 +{v}%',
            'heavy_suppression': '依目前 HP 四分位提高傷害，每一分位 +{v}%',
            'vengeance_mark': '每回合首次受直接傷害疊 1 層（最多 5）；下次傷害行動每層 +{v}% 並消耗',
            'guardian_oath': '護衛中的隊友每回合首次受擊疊 1 層（最多 3）；盾擊每層 +{v}% 並消耗',
            'immovable_wall': '每場以 3 層城牆開始，每層減傷 {v}%；受到直接傷害失去 2 層',
            'steel_echo': '嘲諷中命中可疊層（最多 5）；騎士衝鋒每層 +{v}% 並消耗',
            'life_lance': '自我治療成功時疊層（最多 5）；騎士衝鋒每層 +{v}% 並消耗',
            'endless_arrow': '每次命中疊 1 層（最多 10），每層傷害 +{v}%；未命中失去 2 層',
            'focused_shot': '連續攻擊同一目標時疊層（最多 5），每層傷害 +{v}%；換目標重置',
            'venom_amplifier': '命中帶有毒箭的目標時疊層（最多 5），每層傷害 +{v}%',
            'hunting_rhythm': '暴擊命中疊層（最多 3）；滿層時下次傷害 +{triple}% 並消耗',
            'heart_calibration': '未暴擊的命中疊層（最多 5），每層暴擊率 +{v} 百分點；暴擊後重置',
            'grace_reserve': '治療行動成功時疊層（最多 3）；下次治療每層 +{v}% 並消耗',
            'holy_afterglow': '治療行動成功時疊層（最多 5）；下次傷害每層 +{v}% 並消耗',
            'suffering_prayer': '隊友首次降至 40% HP 以下時疊層（最多 3）；下次治療每層 +{v}% 並消耗',
            'pure_faith': '每淨化一個效果疊 1 層（最多 5），每層治療與聖光效果 +{v}%',
            'threefold_cast': '依序完成治療、祝福、淨化後，下次行動效果 +{v}% 且冷卻少 1 回合',
        }
        effect = crystal.effect_keys[0]
        return templates.get(effect, crystal_affix_name(crystal) + ' +{v}').format(
            v=value, triple=value * 3)
    labels = {
        'HP': 'HP', '攻擊': '攻擊', '防禦': '防禦', '治療量': '治療量',
        'accuracy': '命中值', 'critical_points': '暴擊率', 'evasion_points': '閃避值',
        'speed': '速度', 'poison_damage_percent': '對中毒傷害',
        'break_damage_percent': '對破防傷害', 'low_enemy_damage_percent': '收尾傷害',
        'high_hp_damage_percent': '高 HP 傷害', 'opening_shield_percent': '開場護盾',
        'low_hp_reduction_percent': '低 HP 減傷', 'kill_heal_percent': '擊殺回復',
        'cleanse_heal_percent': '淨化回復', 'lifesteal_percent': '吸血',
        'healing_received_percent': '受治療量', 'survive_fatal_once': '致命留白',
    }
    parts = []
    for effect, value in zip(crystal.effect_keys, crystal.rolled_values):
        label = labels.get(effect, crystal_affix_name(crystal))
        suffix = ('%' if effect.endswith('_percent') else
                  '百分點' if effect in ('critical_points', 'evasion_points') else '')
        if effect == 'survive_fatal_once':
            parts.append('每場一次抵擋致命傷害')
        else:
            parts.append(f'{label} +{value}{suffix}')
    return '、'.join(parts)


@dataclass(frozen=True)
class CrystalInstance:
    instance_id: int
    guild_id: int
    user_id: int
    crystal_type: str
    quality: str
    affix_id: str
    effect_keys: tuple
    rolled_values: tuple
    job: str | None
    source_painting_id: str
    source_room_id: str
    source_user_id: int
    source_stage: int
    created_at: int
    equipment_instance_id: int | None = None
    socket_index: int | None = None

    @property
    def name(self):
        return f'{self.quality}・{CRYSTAL_TYPES[self.crystal_type]}'


class CrystalStore:
    def __init__(self, store):
        self.db = store.db
        from core.rpg_affinity import initialize_affinity
        initialize_affinity(self.db)
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_crystal_instances (
                instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                crystal_type TEXT NOT NULL, quality TEXT NOT NULL,
                affix_id TEXT NOT NULL, effect_keys TEXT NOT NULL, rolled_values TEXT NOT NULL,
                job TEXT, source_painting_id TEXT NOT NULL, source_room_id TEXT NOT NULL,
                source_user_id INTEGER NOT NULL, source_stage INTEGER NOT NULL,
                reward_slot INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                equipment_instance_id INTEGER, socket_index INTEGER)''')
            self.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS rpg_crystal_stage_reward
                ON rpg_crystal_instances(source_room_id,source_stage,source_user_id,reward_slot)''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_crystals_owner
                ON rpg_crystal_instances(guild_id,user_id,equipment_instance_id)''')
            self.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS rpg_crystal_socket
                ON rpg_crystal_instances(equipment_instance_id,socket_index)
                WHERE equipment_instance_id IS NOT NULL''')

    @staticmethod
    def _instance(row):
        if not row:
            return None
        return CrystalInstance(
            row[0], row[1], row[2], row[3], row[4], row[5],
            tuple(json.loads(row[6])), tuple(json.loads(row[7])), row[8], row[9],
            row[10], row[11], row[12], row[13], row[14], row[15])

    @staticmethod
    def _columns():
        return ('instance_id,guild_id,user_id,crystal_type,quality,affix_id,'
                'effect_keys,rolled_values,job,source_painting_id,source_room_id,'
                'source_user_id,source_stage,created_at,equipment_instance_id,socket_index')

    def get(self, instance_id):
        return self._instance(self.db.execute(
            f'SELECT {self._columns()} FROM rpg_crystal_instances WHERE instance_id=?',
            (instance_id,)).fetchone())

    def inventory(self, guild_id, user_id, *, include_socketed=True):
        suffix = '' if include_socketed else ' AND equipment_instance_id IS NULL'
        rows = self.db.execute(
            f'''SELECT {self._columns()} FROM rpg_crystal_instances
                WHERE guild_id=? AND user_id=?{suffix} ORDER BY instance_id''',
            (guild_id, user_id)).fetchall()
        return [self._instance(row) for row in rows]

    @staticmethod
    def _quality(rng):
        return rng.choices(tuple(QUALITY_WEIGHTS), weights=tuple(QUALITY_WEIGHTS.values()), k=1)[0]

    @staticmethod
    def _scaled_roll(rng, low, high, quality):
        multiplier = QUALITY_MULTIPLIERS[quality]
        return max(1, round(rng.randint(low, high) * multiplier))

    def _roll(self, crystal_type, job, rng):
        quality = self._quality(rng)
        if crystal_type == 'outline':
            affix_id = rng.choice(tuple(OUTLINE_AFFIXES))
            _name, effects, lows, highs = OUTLINE_AFFIXES[affix_id]
            values = tuple(self._scaled_roll(rng, low, high, quality)
                           for low, high in zip(lows, highs))
            return quality, affix_id, effects, values, None
        pool = COLOR_AFFIXES if crystal_type == 'color' else SOURCE_AFFIXES.get(job)
        if not pool:
            raise CharacterError('源色結晶需要有效的參戰職業。')
        affix_id = rng.choice(tuple(pool))
        _name, effect, low, high = pool[affix_id]
        value = self._scaled_roll(rng, low, high, quality)
        return quality, affix_id, (effect,), (value,), job if crystal_type == 'source' else None

    def seal_stage(self, room_id, stage, *, now=None):
        """Grant every member one deterministic crystal and mark the room atomically."""
        crystal_type = STAGE_CRYSTAL_TYPES.get(stage)
        if not crystal_type:
            raise CharacterError('無效的繪境迷宮階段。')
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
            per_stage = max(1, len(room['paintings']) // 3)
            if room.get('stage', 0) < stage or room.get('boss_index', 0) < stage * per_stage:
                raise CharacterError('這個階段尚未完成，不能封存結晶。')
            painting = room['paintings'][stage * per_stage - 1]
            jobs = {participant['id']: participant['state']['job']
                    for participant in room['participants']}
            granted = []
            for user_id in room['members']:
                if room.get('loot_percent', 100) == 50:
                    stages = list(range(1, room['stage'] + 1))
                    random.Random(f'{room["seed"]}:retained:{user_id}').shuffle(stages)
                    if stage not in stages[:(len(stages) + 1) // 2]:
                        continue
                existing = self.db.execute('''SELECT instance_id FROM rpg_crystal_instances
                    WHERE source_room_id=? AND source_stage=? AND source_user_id=? AND reward_slot=0''',
                    (room_id, stage, user_id)).fetchone()
                if existing:
                    granted.append(existing[0])
                    continue
                rng = random.Random(f'{room["seed"]}:crystal:{stage}:{user_id}:0')
                quality, affix_id, effects, values, job = self._roll(
                    crystal_type, jobs.get(user_id), rng)
                cursor = self.db.execute('''INSERT INTO rpg_crystal_instances
                    (guild_id,user_id,crystal_type,quality,affix_id,effect_keys,rolled_values,
                     job,source_painting_id,source_room_id,source_user_id,source_stage,
                     reward_slot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (room['guild_id'], user_id, crystal_type, quality, affix_id,
                     json.dumps(effects, ensure_ascii=False), json.dumps(values), job,
                     painting['id'], room_id, user_id, stage, 0, now))
                granted.append(cursor.lastrowid)
            marker = next((reward for reward in room.get('sealed_rewards', [])
                           if reward.get('kind') == 'crystals' and reward.get('stage') == stage), None)
            if marker is None:
                room.setdefault('sealed_rewards', []).append({
                    'kind': 'crystals', 'stage': stage, 'crystal_type': crystal_type,
                    'instance_ids': granted, 'sealed_at': now,
                })
                self.db.execute('''UPDATE rpg_painted_maze_rooms SET data=? WHERE id=?''',
                                (json.dumps(room, ensure_ascii=False), room_id))
            return [self.get(instance_id) for instance_id in granted]

    def seal_shadow_bonus(self, room_id, *, now=None):
        """Grant three deterministic random crystals per member after a Shadow clear."""
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
            if room.get('route') != 'shadow' or room.get('status') != 'completed':
                raise CharacterError('尚未擊敗繪畫之影，不能封存額外結晶。')
            jobs = {participant['id']: participant['state']['job']
                    for participant in room['participants']}
            granted = []
            for user_id in room['members']:
                for reward_slot in range(3):
                    existing = self.db.execute('''SELECT instance_id FROM rpg_crystal_instances
                        WHERE source_room_id=? AND source_stage=4 AND source_user_id=?
                        AND reward_slot=?''', (room_id, user_id, reward_slot)).fetchone()
                    if existing:
                        granted.append(existing[0])
                        continue
                    rng = random.Random(
                        f'{room["seed"]}:shadow-bonus:{user_id}:{reward_slot}')
                    crystal_type = rng.choice(tuple(CRYSTAL_TYPES))
                    quality, affix_id, effects, values, job = self._roll(
                        crystal_type, jobs.get(user_id), rng)
                    cursor = self.db.execute('''INSERT INTO rpg_crystal_instances
                        (guild_id,user_id,crystal_type,quality,affix_id,effect_keys,rolled_values,
                         job,source_painting_id,source_room_id,source_user_id,source_stage,
                         reward_slot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (room['guild_id'], user_id, crystal_type, quality, affix_id,
                         json.dumps(effects, ensure_ascii=False), json.dumps(values), job,
                         'painting_shadow', room_id, user_id, 4, reward_slot, now))
                    granted.append(cursor.lastrowid)
            marker = next((reward for reward in room.get('sealed_rewards', [])
                           if reward.get('kind') == 'shadow_bonus'), None)
            if marker is None:
                room.setdefault('sealed_rewards', []).append({
                    'kind': 'shadow_bonus', 'instance_ids': granted, 'sealed_at': now,
                })
                self.db.execute('UPDATE rpg_painted_maze_rooms SET data=? WHERE id=?',
                                (json.dumps(room, ensure_ascii=False), room_id))
            return [self.get(instance_id) for instance_id in granted]

    def transfer(self, guild_id, user_id, instance_id, recipient_id):
        if recipient_id == user_id:
            raise CharacterError('不能把結晶給予自己。')
        with self.db:
            moved = self.db.execute('''UPDATE rpg_crystal_instances SET user_id=?
                WHERE instance_id=? AND guild_id=? AND user_id=?
                AND equipment_instance_id IS NULL''',
                (recipient_id, instance_id, guild_id, user_id))
            if not moved.rowcount:
                raise CharacterError('找不到可給予的未鑲嵌結晶。')
        return self.get(instance_id)

    @staticmethod
    def _equipment_id(reference):
        if isinstance(reference, int):
            return reference
        if isinstance(reference, str) and reference.startswith('instance:'):
            try:
                return int(reference.split(':', 1)[1])
            except ValueError:
                return None
        return None

    def socket(self, guild_id, user_id, crystal_id, equipment_reference, *, replace_existing=False):
        """Socket for free; replacing destroys the old crystal after validation."""
        equipment_id = self._equipment_id(equipment_reference)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            crystal = self.get(crystal_id)
            equipment = self.db.execute('''SELECT item_id FROM rpg_equipment_instances
                WHERE instance_id=? AND guild_id=? AND user_id=?''',
                (equipment_id, guild_id, user_id)).fetchone()
            item = ITEMS.get(equipment[0]) if equipment else None
            if not crystal or crystal.guild_id != guild_id or crystal.user_id != user_id:
                raise CharacterError('找不到這顆顏料結晶。')
            if crystal.equipment_instance_id is not None:
                raise CharacterError('這顆顏料結晶已經鑲嵌。')
            if not item or crystal.crystal_type not in item.crystal_slots:
                raise CharacterError('這件裝備沒有對應的顏料結晶槽。')
            if crystal.crystal_type == 'source' and crystal.job != item.job:
                raise CharacterError('源色結晶的職業與裝備不符。')
            socket_index = item.crystal_slots.index(crystal.crystal_type)
            existing = self.db.execute('''SELECT instance_id FROM rpg_crystal_instances
                WHERE equipment_instance_id=? AND socket_index=?''',
                (equipment_id, socket_index)).fetchone()
            if existing and not replace_existing:
                raise CharacterError('這個槽位已有結晶；請選擇付費拆除或直接覆蓋。')
            if existing:
                self.db.execute('DELETE FROM rpg_crystal_instances WHERE instance_id=?', existing)
            updated = self.db.execute('''UPDATE rpg_crystal_instances
                SET equipment_instance_id=?,socket_index=?
                WHERE instance_id=? AND guild_id=? AND user_id=?
                AND equipment_instance_id IS NULL''',
                (equipment_id, socket_index, crystal_id, guild_id, user_id))
            if not updated.rowcount:
                raise CharacterError('顏料結晶狀態已經改變，請重新整理。')
        return self.get(crystal_id)

    def remove(self, guild_id, user_id, equipment_reference, crystal_type):
        equipment_id = self._equipment_id(equipment_reference)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            equipment = self.db.execute('''SELECT item_id FROM rpg_equipment_instances
                WHERE instance_id=? AND guild_id=? AND user_id=?''',
                (equipment_id, guild_id, user_id)).fetchone()
            item = ITEMS.get(equipment[0]) if equipment else None
            if not item or crystal_type not in item.crystal_slots:
                raise CharacterError('這件裝備沒有對應的顏料結晶槽。')
            socket_index = item.crystal_slots.index(crystal_type)
            crystal_row = self.db.execute('''SELECT instance_id FROM rpg_crystal_instances
                WHERE equipment_instance_id=? AND socket_index=? AND user_id=?''',
                (equipment_id, socket_index, user_id)).fetchone()
            if not crystal_row:
                raise CharacterError('這個槽位目前沒有結晶。')
            from core.rpg_affinity import tailoring_price
            price = tailoring_price(self.db, guild_id, user_id, CRYSTAL_REMOVAL_PRICE)
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''',
                (price, guild_id, user_id, price))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，完整拆除需要 {price:,} 金幣。')
            record_gold(self.db, guild_id, user_id, -price, 'crystal_removal', str(crystal_row[0]))
            self.db.execute('''UPDATE rpg_crystal_instances
                SET equipment_instance_id=NULL,socket_index=NULL WHERE instance_id=?''', crystal_row)
        return self.get(crystal_row[0])

    def sell(self, guild_id, user_id, instance_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            crystal = self.get(instance_id)
            if (not crystal or crystal.guild_id != guild_id or crystal.user_id != user_id
                    or crystal.equipment_instance_id is not None):
                raise CharacterError('找不到可出售的未鑲嵌結晶。')
            price = QUALITY_SELL_PRICES[crystal.quality]
            self.db.execute('DELETE FROM rpg_crystal_instances WHERE instance_id=?', (instance_id,))
            self.db.execute('''INSERT INTO rpg_wallets(guild_id,user_id,gold) VALUES (?,?,?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold''',
                (guild_id, user_id, price))
            record_gold(self.db, guild_id, user_id, price, 'crystal_sale', str(instance_id))
            return price
