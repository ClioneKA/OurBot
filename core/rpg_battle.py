"""Deterministic when seeded: automatic turn combat and persistent skill tactics."""
from dataclasses import dataclass, field
from decimal import Decimal
import random

from core.rpg_character import CharacterError, ITEMS, combat_from_stats, speed_from_equipment
from core.rpg_monsters import REFERENCE_LEVELS, monster_name
from core.rpg import level_for


CONDITIONS = {'always': '可用就施放', 'self40': '自身 HP ≤ 指定比例',
              'ally50': '隊伍有人 HP ≤ 指定比例', 'allies_injured': '受傷隊友數量 ≥ 指定人數',
              'enemies3': '存活敵人數量 ≥ 指定數量',
              'enemy_hp_lte': '任一敵人 HP ≤ 指定比例',
              'enemy_hp_gte': '任一敵人 HP ≥ 指定比例',
              'round_gte': '戰鬥回合 ≥ 指定回合',
              'ally_debuff': '隊友有可淨化負面狀態（含自己）',
              'enemy_charging': '敵人正在蓄力'}
CONDITION_LIMITS = {
    'self40': (1, 100, 40, '%'),
    'ally50': (1, 100, 50, '%'),
    'allies_injured': (1, 20, 2, ' 人'),
    'enemies3': (1, 3, 3, ' 隻'),
    'enemy_hp_lte': (1, 100, 30, '%'),
    'enemy_hp_gte': (1, 100, 70, '%'),
    'round_gte': (1, 30, 5, ' 回合'),
}
TARGETS = {'lowest': '血量比例最低', 'strongest': '攻擊最高', 'self': '自己',
           'debuffed': '有可淨化負面狀態的隊友'}


def empty_combat_stats():
    """Per-fighter counters kept in battle snapshots for settlement and analysis."""
    return dict(damage_dealt=0, direct_damage=0, support_damage=0,
                damage_taken=0, healing_done=0, healing_received=0,
                overhealing=0, attacks=0, hits=0, misses=0, critical_hits=0,
                knockouts=0, deaths=0, skills_used={})


@dataclass(frozen=True)
class Skill:
    name: str
    effect: str
    cooldown: int
    description: str
    condition: str = 'always'
    timing: str = 'normal'


PREPARATION_TIMING = 'preparation'


def skill_description(skill):
    prefix = '【準備】' if skill.timing == PREPARATION_TIMING else ''
    return f'{prefix}{skill.description}'


SKILLS = {
    '民兵': (Skill('奮力一擊', 'strike', 2, '造成 160% 傷害'),
             Skill('包紮', 'heal', 3, '以治療量的 50% 恢復一名隊友生命', 'ally50'),
             Skill('防禦', 'stance', 3, '自身減傷 20%，持續至下一回合結束', 'self40',
                   timing=PREPARATION_TIMING)),
    '裝甲步兵': (Skill('重擊', 'strike', 2, '造成 160% 傷害'),
                 Skill('破甲', 'break', 3, '造成 100% 傷害並使目標防禦歸零，持續至下一回合結束',
                       timing=PREPARATION_TIMING),
                 Skill('攻守架勢', 'stance', 3, '自身減傷 35%、攻擊提升 20%，持續至下一回合結束', 'self40',
                       timing=PREPARATION_TIMING),
                 Skill('橫掃斬', 'cleave', 4, '對全體敵人造成 120% 傷害'),
                 Skill('重裝猛擊', 'crush', 4, '對單一敵人造成 220% 傷害')),
    '騎士': (Skill('嘲諷', 'taunt', 3, '吸引敵方單體攻擊並使自身減傷 15%，持續至下一回合結束',
                   timing=PREPARATION_TIMING),
             Skill('護衛', 'guard', 3, '全隊防禦增加施放者最大 HP 的 5%，同效果取較強值，持續至下一回合結束', 'ally50',
                   timing=PREPARATION_TIMING),
             Skill('堅守', 'stance', 3, '自身減傷 50%，持續至下一回合結束', 'self40',
                   timing=PREPARATION_TIMING),
             Skill('盾擊', 'shield_bash', 4, '造成 120% 傷害，命中後打斷蓄力並暈眩至下一回合結束（跳過一次行動）'),
             Skill('重整旗鼓', 'rally', 4, '恢復自身最大 HP 的 25%', 'self40')),
    '弓兵': (Skill('連射', 'double', 2, '兩次 85% 傷害，各自判定命中'),
             Skill('精準射擊', 'precise', 3, '必中，造成 150% 傷害'),
             Skill('箭雨', 'area', 4, '對所有敵人造成 80% 傷害', 'enemies3'),
             Skill('三連矢', 'triple', 4, '對單一敵人連射三次，每次 75% 傷害，分別判定命中'),
             Skill('毒箭', 'poison_arrow', 3, '造成 120% 傷害，命中後中毒至後兩回合結束；行動前損失最大 HP 的 2%（無條件捨去，最低 1）')),
    '僧侶': (Skill('治療', 'heal', 2, '恢復一名隊友生命', 'ally50'),
             Skill('祝福', 'bless', 3, '提升一名隊友攻擊 25%，持續至下一回合結束',
                   timing=PREPARATION_TIMING),
             Skill('淨化', 'cleanse', 2, '移除一名隊友的中毒、破甲與暈眩', 'ally_debuff'),
             Skill('群體治療', 'group_heal', 4, '恢復全體存活隊友各 65% 治療量的 HP', 'ally50'),
             Skill('強效治療', 'greater_heal', 4, '恢復一名隊友 180% 治療量的 HP', 'ally50')),
}
ALLY_EFFECTS = {'heal', 'guard', 'bless', 'cleanse', 'group_heal', 'greater_heal'}
FIXED_TARGETS = {'guard': '全隊', 'group_heal': '全隊', 'area': '全體敵人',
                 'cleave': '全體敵人', 'stance': '自己', 'taunt': '自己', 'rally': '自己'}
# Defense is a role property of the target. Monsters and non-tank professions
# retain the original coefficient.
DEFENSE_EFFECTIVENESS = {'裝甲步兵': 0.40, '騎士': 0.45}


def unlocked_skills(job, level):
    return SKILLS[job] if level >= 20 else SKILLS[job][:3]


def rule_skill(job, rule):
    # Old saved tactics and battle snapshots used the slot as the skill ID.
    return SKILLS[job][(rule.skill_id or rule.slot) - 1]


def default_target(skill):
    if skill.effect == 'cleanse':
        return 'debuffed'
    if skill.effect == 'bless':
        return 'strongest'
    return 'lowest'


def condition_value(condition, value=None):
    """Return a validated threshold, using the legacy condition's default when absent."""
    if condition not in CONDITION_LIMITS:
        return None
    low, high, default, _ = CONDITION_LIMITS[condition]
    value = default if value is None else value
    if type(value) is not int or not low <= value <= high:
        raise CharacterError(f'施放條件數值必須介於 {low}–{high}。')
    return value


def condition_text(condition, value=None):
    if condition not in CONDITION_LIMITS:
        return CONDITIONS[condition]
    threshold = condition_value(condition, value)
    suffix = CONDITION_LIMITS[condition][3]
    labels = {
        'self40': f'自身 HP ≤ {threshold}{suffix}',
        'ally50': f'隊伍有人 HP ≤ {threshold}{suffix}',
        'allies_injured': f'受傷隊友數量 ≥ {threshold}{suffix}',
        'enemies3': f'存活敵人數量 ≥ {threshold}{suffix}',
        'enemy_hp_lte': f'任一敵人 HP ≤ {threshold}{suffix}',
        'enemy_hp_gte': f'任一敵人 HP ≥ {threshold}{suffix}',
        'round_gte': f'戰鬥回合 ≥ {threshold}',
    }
    return labels[condition]


@dataclass(frozen=True)
class Rule:
    slot: int
    priority: int
    enabled: bool
    condition: str
    target: str
    skill_id: int | None = None
    condition_value: int | None = None


def default_rules(job):
    return [Rule(i, i, True, skill.condition, default_target(skill),
                 condition_value=condition_value(skill.condition))
            for i, skill in enumerate(SKILLS[job][:3], 1)]


class Tactics:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_tactics (
                guild_id INTEGER, user_id INTEGER, job TEXT, slot INTEGER,
                priority INTEGER, enabled INTEGER, condition TEXT, target TEXT,
                PRIMARY KEY (guild_id, user_id, job, slot))''')
            if 'skill_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(rpg_tactics)')}:
                self.db.execute('ALTER TABLE rpg_tactics ADD COLUMN skill_id INTEGER')
            if 'condition_value' not in {row[1] for row in self.db.execute('PRAGMA table_info(rpg_tactics)')}:
                self.db.execute('ALTER TABLE rpg_tactics ADD COLUMN condition_value INTEGER')

    def available(self, guild, user, job):
        return unlocked_skills(job, level_for(self.store.xp(guild, user)))

    def rules(self, guild, user, job):
        saved = {row[0]: Rule(row[0], row[1], bool(row[2]), row[3], row[4], row[5],
                              condition_value(row[3], row[6])) for row in self.db.execute(
            'SELECT slot, priority, enabled, condition, target, skill_id, condition_value FROM rpg_tactics '
            'WHERE guild_id=? AND user_id=? AND job=?', (guild, user, job))}
        return sorted([saved.get(rule.slot, rule) for rule in default_rules(job)], key=lambda rule: rule.priority)

    def configure(self, guild, user, job, slot, priority, enabled, condition, target, threshold=None):
        if job not in SKILLS or slot not in (1, 2, 3) or priority not in (1, 2, 3):
            raise CharacterError('技能槽與優先順序必須是 1–3。')
        if condition not in CONDITIONS or target not in TARGETS or type(enabled) is not bool:
            raise CharacterError('無效的自動施放設定。')
        rules = self.rules(guild, user, job)
        current = next(rule for rule in rules if rule.slot == slot)
        threshold = condition_value(condition,
                                    current.condition_value if threshold is None and condition == current.condition else threshold)
        skill = rule_skill(job, current)
        if target == 'self' and skill.effect not in ALLY_EFFECTS | {'stance', 'taunt', 'rally'}:
            raise CharacterError('攻擊技能不能以自己為目標。')
        if target == 'debuffed' and skill.effect != 'cleanse':
            raise CharacterError('負面狀態目標僅供淨化使用。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rules = self.rules(guild, user, job)
            old_priority = next(rule.priority for rule in rules if rule.slot == slot)
            updated = []
            for rule in rules:
                if rule.slot == slot:
                    rule = Rule(slot, priority, enabled, condition, target, rule.skill_id, threshold)
                elif rule.priority == priority:
                    rule = Rule(rule.slot, old_priority, rule.enabled, rule.condition, rule.target,
                                rule.skill_id, rule.condition_value)
                updated.append((guild, user, job, rule.slot, rule.priority, int(rule.enabled), rule.condition,
                                rule.target, rule.skill_id, rule.condition_value))
            self.db.executemany('INSERT OR REPLACE INTO rpg_tactics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', updated)

    def equip(self, guild, user, job, slot, skill_id):
        if job not in SKILLS or slot not in (1, 2, 3):
            raise CharacterError('無效的職業或技能槽。')
        if type(skill_id) is not int or not 1 <= skill_id <= len(self.available(guild, user, job)):
            raise CharacterError('技能尚未解鎖；進階技能需要 Lv.20。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rules = self.rules(guild, user, job)
            current = next(rule for rule in rules if rule.slot == slot)
            if (current.skill_id or current.slot) == skill_id:
                return
            if any((rule.skill_id or rule.slot) == skill_id for rule in rules if rule.slot != slot):
                raise CharacterError('此技能已裝備於其他格，請先替換該格技能。')
            skill = SKILLS[job][skill_id - 1]
            self.db.execute('INSERT OR REPLACE INTO rpg_tactics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (guild, user, job, slot, current.priority, int(current.enabled), skill.condition,
                             default_target(skill), skill_id, condition_value(skill.condition)))


@dataclass
class Fighter:
    name: str
    team: int
    job: str
    stats: dict
    speed: int
    rules: list
    hp: int = field(init=False)
    ready: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
    effect_sources: dict = field(default_factory=dict)
    guard_bonus: int = 0
    stability: tuple = (100, 100)
    armed: bool = True
    lifesteal: int = 0
    damage_guard_chance: int = 0
    vulnerable_chance: int = 0
    vulnerable_percent: int = 0
    healing_share: int = 0
    status_stacks: dict = field(default_factory=dict)
    user_id: int | None = None
    combat_stats: dict = field(default_factory=empty_combat_stats)
    food_name: str = ''
    food_heal_permille: int = 0
    food_regen_permille: int = 0
    food_regen_rounds: int = 0
    food_used: bool = False
    food_regen_left: int = 0
    food_regen_start: int = 0
    fortune_card: str = ''
    damage_dealt_percent: int = 0
    damage_taken_percent: int = 0
    healing_received_percent: int = 0
    cooldown_reduction: int = 0
    linked_user_id: int | None = None

    def __post_init__(self):
        # Upgrade persisted battles from the former physical/magic stat split.
        self.stats = dict(self.stats)
        if '攻擊' not in self.stats:
            self.stats['攻擊'] = self.stats['物攻'] + (self.stats.get('法防', 0) if self.team == 0 else 0)
        if '防禦' not in self.stats:
            self.stats['防禦'] = self.stats['物防']
        for obsolete in ('物攻', '物防', '法攻', '法防'):
            self.stats.pop(obsolete, None)
        self.hp = self.stats['HP']

    def has(self, effect, turn):
        return self.effects.get(effect, -1) >= turn

    @property
    def dexterity(self):
        """Compatibility alias for code holding a pre-migration fighter."""
        return self.speed

    @dexterity.setter
    def dexterity(self, value):
        self.speed = value


class Battle:
    def __init__(self, fighters, seed=None, max_rounds=30):
        self.fighters = fighters
        self.rng = random.Random(seed)
        self.round = 0
        self.max_rounds = max_rounds
        self.result = None
        self.log = []
        self.mechanics = {}

    @staticmethod
    def record_skill(actor, name):
        used = actor.combat_stats['skills_used']
        used[name] = used.get(name, 0) + 1

    def accuracy_bonus(self, actor):
        return 0

    def critical_bonus(self, actor):
        return 0

    def damage_dealt_multiplier(self, actor):
        return (100 + actor.damage_dealt_percent) / 100

    def damage_taken_multiplier(self, target):
        percent = target.damage_taken_percent
        if target.has('fortune_hermit_guard', self.round):
            percent -= 25
        return max(0, 100 + percent) / 100

    def healing_done_multiplier(self, actor):
        return 1.0

    def healing_received_multiplier(self, target):
        return max(0, 100 + target.healing_received_percent) / 100

    def heal(self, actor, target, requested, trigger_share=True):
        requested = max(0, int(requested * self.healing_done_multiplier(actor)
                               * self.healing_received_multiplier(target)))
        amount = min(target.stats['HP'] - target.hp, requested)
        target.hp += amount
        actor.combat_stats['healing_done'] += amount
        actor.combat_stats['overhealing'] += requested - amount
        target.combat_stats['healing_received'] += amount
        if amount and trigger_share and target.team == 0 and target.status_stacks.get('corruption', 0):
            beast = next((f for f in self.living(1) if f.job == '瘟疫縫合獸'), None)
            if beast is not None:
                rate = 30 if self.mechanics.get('plague_phase_two') else 20
                stolen = self.restore(beast, amount * rate // 100)
                if stolen:
                    self.log.append(f'{beast.name} 的【共享血肉】恢復 {stolen} HP。')
        if amount and trigger_share and target.healing_share:
            allies = [f for f in self.living(target.team) if f is not target and f.hp < f.stats['HP']]
            if allies:
                ally = min(allies, key=lambda f: f.hp / f.stats['HP'])
                shared = self.heal(target, ally, amount * target.healing_share // 100, trigger_share=False)
                if shared:
                    self.log.append(f'{target.name} 的【雙生護符】使 {ally.name} 恢復 {shared} HP。')
        return amount

    @staticmethod
    def restore(target, requested):
        """Restore HP without attributing skill healing to another fighter."""
        amount = min(target.stats['HP'] - target.hp, max(0, int(requested)))
        target.hp += amount
        target.combat_stats['healing_received'] += amount
        return amount

    def maybe_eat(self, target):
        if (target.team != 0 or target.food_used or not target.food_name or target.hp <= 0
                or target.hp * 100 > target.stats['HP'] * 40):
            return
        target.food_used = True
        amount = self.restore(target, max(1, target.stats['HP'] * target.food_heal_permille // 1000))
        self.log.append(f'{target.name} 食用【{target.food_name}】，恢復 {amount} HP。')
        if target.food_regen_permille and target.food_regen_rounds:
            target.food_regen_left = target.food_regen_rounds
            target.food_regen_start = self.round + 1

    def apply_damage(self, target, damage, share_link=True):
        """Apply final damage, including tarot survival and Lovers sharing."""
        partner = None
        if share_link and target.team == 0 and target.linked_user_id is not None:
            partner = next((fighter for fighter in self.living(0)
                            if fighter.user_id == target.linked_user_id and fighter is not target), None)

        def apply_one(victim, requested):
            before = victim.hp
            actual = min(before, max(0, int(requested)))
            if (actual >= before and before > 0 and victim.fortune_card == 'judgement'
                    and not victim.effects.get('fortune_judgement_used')):
                actual = max(0, before - 1)
                victim.effects['fortune_judgement_used'] = True
                self.log.append(f'{victim.name} 的【審判】生效，在致死傷害中保留 1 HP。')
            victim.hp -= actual
            victim.combat_stats['damage_taken'] += actual
            if actual and victim.hp == 0:
                victim.combat_stats['deaths'] += 1
            if (before * 100 > victim.stats['HP'] * 40
                    and victim.hp * 100 <= victim.stats['HP'] * 40
                    and victim.fortune_card == 'hermit'
                    and not victim.effects.get('fortune_hermit_used')):
                victim.effects['fortune_hermit_used'] = True
                victim.effects['fortune_hermit_guard'] = self.round + 1
                self.log.append(f'{victim.name} 的【隱者】生效，獲得 25% 減傷至第 {self.round + 1} 回合結束。')
            return actual

        if partner is None:
            target_actual, partner_actual = apply_one(target, damage), 0
        else:
            target_actual = apply_one(target, (damage + 1) // 2)
            partner_actual = apply_one(partner, damage // 2)
            self.log.append(f'{target.name} 與 {partner.name} 的【戀人】連結分攤傷害：'
                            f'{target_actual}／{partner_actual} HP。')
        self.maybe_eat(target)
        if partner is not None:
            self.maybe_eat(partner)
        return target_actual, partner, partner_actual

    def living(self, team):
        return [f for f in self.fighters if f.team == team and f.hp > 0]

    def action_priority(self, actor):
        """Resolve proactive player buffs in a preparation phase before attacks."""
        if actor.team != 0:
            return 0
        selected = self.select(actor)
        return int(bool(selected and selected[1].timing == PREPARATION_TIMING))

    def effect_source(self, target, effect):
        source_id = target.effect_sources.get(effect)
        if source_id is None:
            return None
        return next((fighter for fighter in self.fighters if fighter.user_id == source_id), None)

    def check_end(self):
        master = next((f for f in self.fighters if f.team == 1 and f.job == '王城傀儡師'), None)
        if master is not None and master.hp <= 0:
            for puppet in (f for f in self.fighters if f.team == 1 and f.job in ('劍傀儡', '咒傀儡')):
                puppet.hp = 0
        if not self.living(0) and not self.living(1):
            self.result = '平手'
        elif not self.living(1):
            self.result = '勝利'
        elif not self.living(0):
            self.result = '戰敗'
        return self.result is not None

    def target(self, actor, candidates, rule, offensive=False):
        if rule.target == 'debuffed':
            candidates = [f for f in candidates if any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun'))
                          or f.status_stacks.get('corruption', 0)]
        if offensive:
            taunters = [f for f in candidates if f.has('taunt', self.round)]
            candidates = taunters or candidates
        if rule.target == 'self':
            return actor if actor in candidates else None
        if not candidates:
            return None
        if rule.target == 'debuffed':
            return max(candidates, key=lambda f: (f.status_stacks.get('corruption', 0),
                                                   -f.hp / f.stats['HP']))
        if rule.target == 'strongest':
            return max(candidates, key=lambda f: f.stats['攻擊'])
        return min(candidates, key=lambda f: f.hp / f.stats['HP'])

    def select(self, actor):
        allies, enemies = self.living(actor.team), self.living(1 - actor.team)
        for rule in sorted(actor.rules, key=lambda r: r.priority):
            skill = rule_skill(actor.job, rule)
            if not rule.enabled or self.round < actor.ready.get(rule.slot, 0):
                continue
            threshold = condition_value(rule.condition, rule.condition_value)
            if rule.condition == 'self40' and actor.hp * 100 > actor.stats['HP'] * threshold:
                continue
            if rule.condition == 'ally50' and not any(f.hp * 100 <= f.stats['HP'] * threshold for f in allies):
                continue
            if rule.condition == 'allies_injured' and sum(f.hp < f.stats['HP'] for f in allies) < threshold:
                continue
            if rule.condition == 'enemies3' and len(enemies) < threshold:
                continue
            if rule.condition == 'enemy_hp_lte' and not any(
                    f.hp * 100 <= f.stats['HP'] * threshold for f in enemies):
                continue
            if rule.condition == 'enemy_hp_gte' and not any(
                    f.hp * 100 >= f.stats['HP'] * threshold for f in enemies):
                continue
            if rule.condition == 'round_gte' and self.round < threshold:
                continue
            if rule.condition == 'ally_debuff' and not any(
                    any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun'))
                    or f.status_stacks.get('corruption', 0) for f in allies):
                continue
            if rule.condition == 'enemy_charging' and not any(
                    f.has('charged_punch', self.round) or
                    (f.job == '深淵鐘龍' and self.mechanics.get('clock_charging')) or
                    (f.job == '城崎諾亞' and self.mechanics.get('noah_draft_charging')) for f in enemies):
                continue
            candidates = allies if skill.effect in ALLY_EFFECTS else enemies
            if skill.effect in ('heal', 'greater_heal', 'group_heal'):
                candidates = [f for f in candidates if f.hp < f.stats['HP']]
            elif skill.effect == 'cleanse':
                candidates = [f for f in candidates if any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun'))]
            elif skill.effect == 'bless':
                candidates = [f for f in candidates if not f.has(skill.effect, self.round)]
            if skill.effect == 'group_heal':
                target = actor if candidates else None
            elif skill.effect == 'rally':
                target = actor if actor.hp < actor.stats['HP'] else None
            elif skill.effect == 'guard':
                bonus = max(1, actor.stats['HP'] // 20)
                target = actor if any(not f.has('guard', self.round) or f.guard_bonus < bonus for f in allies) else None
            elif skill.effect in ('stance', 'taunt'):
                target = actor if not actor.has(skill.effect, self.round) else None
            else:
                target = self.target(actor, candidates, rule, skill.effect not in ALLY_EFFECTS)
            if target:
                return rule, skill, target
        return None

    def hit(self, actor, target, power=1.0, precise=False, lifesteal=None):
        if actor.team == 0 and not actor.armed:
            self.log.append(f'{actor.name} 未裝備武器，無法造成傷害。')
            return False
        actor.combat_stats['attacks'] += 1
        evasion = target.stats['閃避率'] + (15 if target.has('moon_shadow', self.round) else 0)
        chance = max(10, min(99, actor.stats['命中率'] + self.accuracy_bonus(actor) - evasion))
        if not precise and self.rng.random() * 100 >= chance:
            actor.combat_stats['misses'] += 1
            self.log.append(f'{actor.name} → {target.name}：未命中')
            return False
        base_attack = actor.stats['攻擊']
        base_attack *= 1.2 if actor.job == '裝甲步兵' and actor.has('stance', self.round) else 1
        blessed = actor.has('bless', self.round)
        paint_attack = base_attack * self.damage_dealt_multiplier(actor)
        attack = paint_attack * (1.25 if blessed else 1)
        base_defense = target.stats['防禦']
        if target.has('guard', self.round):
            base_defense += target.guard_bonus
        broken = target.has('break', self.round)
        defense = 0 if broken else base_defense
        low, high = actor.stability
        stability = self.rng.randint(low, high) if low != high else low
        critical = self.rng.random() * 100 < actor.stats['暴擊率'] + self.critical_bonus(actor)
        guarded = bool(target.damage_guard_chance and self.rng.random() * 100 < target.damage_guard_chance)
        puppet_shield = (target.job == '王城傀儡師'
                         and any(f.job == '咒傀儡' for f in self.living(target.team))
                         and not self.mechanics.get('puppet_phase_two'))
        defense_effectiveness = DEFENSE_EFFECTIVENESS.get(target.job, 0.35)

        def final_damage(attack_value, defense_value):
            value = max(1, int(attack_value * power - defense_value * defense_effectiveness))
            value = max(1, value * stability // 100)
            if critical:
                value = int(value * 1.5)
            if target.has('stance', self.round):
                multiplier = {'民兵': 0.8, '騎士': 0.5}.get(target.job, 0.65)
                value = max(1, int(value * multiplier))
            if target.has('taunt', self.round):
                value = max(1, int(value * 0.85))
            if target.has('noah_blue_guard', self.round):
                value = max(1, value * self.mechanics.get('noah_blue_reduction', 0) // 100)
            if puppet_shield:
                value = max(1, value // 2)
            if guarded:
                value = max(1, value // 2)
            value = max(1, int(value * self.damage_taken_multiplier(target)))
            return value

        damage = final_damage(attack, defense)
        base_damage = final_damage(paint_attack, base_defense)
        broken_damage = final_damage(paint_attack, defense)
        vulnerable = target.has('vulnerable', self.round)
        pre_vulnerable_damage = damage
        if vulnerable:
            damage = max(1, damage * 110 // 100)
        actual_damage = min(target.hp, damage)
        pre_vulnerable_actual = min(target.hp, pre_vulnerable_damage)
        base_actual = min(target.hp, base_damage)
        broken_actual = min(target.hp, broken_damage)
        break_assist = max(0, broken_actual - base_actual) if broken else 0
        bless_assist = max(0, pre_vulnerable_actual - broken_actual) if blessed else 0
        vulnerable_assist = max(0, actual_damage - pre_vulnerable_actual) if vulnerable else 0
        target_actual, partner, partner_actual = self.apply_damage(target, damage)
        actual_damage = target_actual + partner_actual
        actor.combat_stats['hits'] += 1
        actor.combat_stats['critical_hits'] += int(critical)
        actor.combat_stats['damage_dealt'] += actual_damage
        remaining_credit = actual_damage
        for effect, amount, affected in (('break', break_assist, target), ('bless', bless_assist, actor),
                                         ('vulnerable', vulnerable_assist, target)):
            amount = min(amount, remaining_credit)
            if not amount:
                continue
            remaining_credit -= amount
            source = self.effect_source(affected, effect)
            if source is None:
                actor.combat_stats['direct_damage'] += amount
            else:
                source.combat_stats['support_damage'] += amount
        actor.combat_stats['direct_damage'] += remaining_credit
        actor.combat_stats['knockouts'] += int(target_actual and target.hp == 0)
        if partner is not None:
            actor.combat_stats['knockouts'] += int(partner_actual and partner.hp == 0)
        self.log.append(f'{actor.name} → {target.name}：{damage} 傷害{"（暴擊）" if critical else ""}'
                        f'{f"（穩定度 {stability}%）" if actor.stability != (100, 100) else ""}'
                        f'{"（套裝減傷）" if guarded else ""}{"，倒下" if target.hp == 0 else ""}')
        if actor.team == 0 and target.job == '深淵鐘龍' and self.mechanics.get('clock_charging'):
            layers = self.mechanics.get('clock_armor', 0)
            if layers > 0:
                removed = 2 if precise or critical else 1
                self.mechanics['clock_armor'] = max(0, layers - removed)
                self.log.append(f'{target.name} 的鐘甲減少 {min(layers, removed)} 層，剩餘 {self.mechanics["clock_armor"]} 層。')
        if (actor.team == 0 and target.hp > 0 and actor.vulnerable_chance
                and self.rng.random() * 100 < actor.vulnerable_chance):
            target.effects['vulnerable'] = self.round + 1
            if actor.user_id is not None:
                target.effect_sources['vulnerable'] = actor.user_id
            else:
                target.effect_sources.pop('vulnerable', None)
            self.log.append(f'{target.name} 陷入易傷，受到的直接傷害 +{actor.vulnerable_percent}% 至第 {self.round + 1} 回合結束。')
        drain = actor.lifesteal if lifesteal is None else lifesteal
        healing = self.heal(actor, actor, actual_damage * drain // 100)
        if healing > 0 and actor.hp > 0:
            self.log.append(f'{actor.name} 吸血恢復 {healing} HP')
        return True

    def add_corruption(self, target, amount=1):
        before = target.status_stacks.get('corruption', 0)
        target.status_stacks['corruption'] = min(3, before + amount)
        self.log.append(f'{target.name} 的腐敗變為 {target.status_stacks["corruption"]} 層（3 層將在行動前爆裂）。')

    def clockwork_act(self, actor):
        if actor.hp * 2 <= actor.stats['HP'] and not self.mechanics.get('clock_phase_two'):
            self.mechanics['clock_phase_two'] = True
            self.log.append(f'{actor.name} 進入第二階段：終末鳴鐘的週期縮短，鐘甲額外 +2 層！')
        if self.mechanics.get('clock_charging'):
            layers = self.mechanics.get('clock_armor', 0)
            self.mechanics['clock_charging'] = False
            if not layers:
                actor.effects['break'] = self.round + 1
                self.log.append(f'{actor.name} 的鐘甲崩解！終末鐘聲取消，遭到破甲至第 {self.round + 1} 回合結束。')
            else:
                power = min(3.0, 0.9 + layers * 0.15)
                self.record_skill(actor, '終末鐘聲')
                self.log.append(f'{actor.name} 釋放【終末鐘聲】：剩餘 {layers} 層鐘甲，全體 {power * 100:g}% 傷害！')
                for enemy in self.living(0):
                    if enemy.hp > 0:
                        self.hit(actor, enemy, power)
            self.mechanics['next_clock_charge'] = self.round + (2 if self.mechanics.get('clock_phase_two') else 3)
            return
        if self.round >= self.mechanics.get('next_clock_charge', 3):
            players = len(self.living(0))
            layers = max(2, players + players // 3) + (2 if self.mechanics.get('clock_phase_two') else 0)
            self.mechanics.update(clock_charging=True, clock_armor=layers)
            self.record_skill(actor, '終末鳴鐘')
            self.log.append(f'{actor.name} 使用【終末鳴鐘】：獲得 {layers} 層鐘甲，下回合將釋放終末鐘聲！')
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None:
            self.hit(actor, target)

    def puppeteer_act(self, actor):
        puppets = [f for f in self.fighters if f.team == 1 and f.job in ('劍傀儡', '咒傀儡')]
        if actor.hp * 2 <= actor.stats['HP'] and not self.mechanics.get('puppet_phase_two'):
            self.mechanics['puppet_phase_two'] = True
            living = [f for f in puppets if f.hp > 0]
            names = {f.job for f in living}
            for puppet in living:
                puppet.hp = 0
            if '劍傀儡' in names:
                actor.stats['攻擊'] = max(1, actor.stats['攻擊'] * 125 // 100)
            if '咒傀儡' in names:
                actor.lifesteal = max(actor.lifesteal, 20)
            gained = '、'.join(sorted(names)) or '沒有存活傀儡'
            self.log.append(f'{actor.name} 使用【最後謝幕】，吸收：{gained}；永久停止重編絲線。')
            return
        if self.mechanics.get('puppet_phase_two'):
            if self.round % 3 == 0:
                self.record_skill(actor, '王城謝幕')
                power = 0.9 + 0.25 * int(actor.stats['攻擊'] > self.mechanics.get('puppet_base_attack', actor.stats['攻擊']))
                power += 0.25 * int(actor.lifesteal >= 20)
                self.log.append(f'{actor.name} 使用【王城謝幕】')
                for enemy in self.living(0):
                    if enemy.hp > 0:
                        self.hit(actor, enemy, power)
                return
        elif self.round % 3 == 0:
            self.record_skill(actor, '重編絲線')
            dead = [f for f in puppets if f.hp <= 0]
            if len(dead) == 2:
                actor.effects['break'] = self.round + 1
                self.log.append(f'{actor.name} 的【重編絲線】失敗，遭到破甲至第 {self.round + 1} 回合結束。')
            elif dead:
                target = dead[0]
                target.hp = max(1, target.stats['HP'] * 30 // 100)
                self.log.append(f'{actor.name} 使用【重編絲線】，{target.name} 以 {target.hp} HP 復活。')
            else:
                target = min(puppets, key=lambda f: f.hp / f.stats['HP'])
                healing = self.restore(target, target.stats['HP'] * 15 // 100)
                self.log.append(f'{actor.name} 使用【重編絲線】，{target.name} 恢復 {healing} HP。')
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None:
            self.hit(actor, target)

    def noah_act(self, actor):
        """Resolve Noah's announced colour and the below-70% composition loop."""
        if actor.hp * 100 <= actor.stats['HP'] * 70 and not self.mechanics.get('noah_phase_two'):
            self.mechanics.update(noah_phase_two=True, noah_color_index=0,
                                  noah_composition=0, noah_draft_charging=False)
            self.log.append(f'{actor.name} 進入第二階段【開始調色】：顏料順序重置為紅 → 黃 → 藍，並開始未完成構圖！')

        if self.mechanics.get('noah_draft_charging'):
            self.mechanics.update(noah_draft_charging=False, noah_composition=0, noah_color_index=0)
            self.record_skill(actor, '未完成稿')
            self.log.append(f'{actor.name} 完成【未完成稿】：對全隊造成 180% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 1.8)
            return

        phase_two = self.mechanics.get('noah_phase_two', False)
        colors = ('red', 'yellow', 'blue')
        color = colors[self.mechanics.get('noah_color_index', 0)] if phase_two else self.mechanics['noah_primary_color']
        color_name = {'red': '紅色', 'yellow': '黃色', 'blue': '藍色'}[color]
        power = {'red': 1.8 if phase_two else 1.5,
                 'yellow': 0.75 if phase_two else 0.6,
                 'blue': 1.3 if phase_two else 1.1}[color]
        self.record_skill(actor, f'{color_name}顏料罐')
        if color == 'yellow':
            self.log.append(f'{actor.name} 潑灑【黃色顏料罐】：對全隊造成 {power * 100:g}% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, power)
        else:
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            self.log.append(f'{actor.name} 使用【{color_name}顏料罐】')
            if target is not None:
                self.hit(actor, target, power)
                if color == 'red' and target.hp > 0:
                    target.effects['break'] = self.round + 1
                    self.log.append(f'{target.name} 遭紅色顏料破甲至第 {self.round + 1} 回合結束。')
            if color == 'blue' and actor.hp > 0:
                healing = self.restore(actor, actor.stats['HP'] * (5 if phase_two else 3) // 100)
                reduction = 30 if phase_two else 20
                self.mechanics['noah_blue_reduction'] = 100 - reduction
                actor.effects['noah_blue_guard'] = self.round + 1
                self.log.append(f'{actor.name} 恢復 {healing} HP，受到傷害 -{reduction}% 至第 {self.round + 1} 回合結束。')

        if phase_two:
            composition = self.mechanics.get('noah_composition', 0) + 1
            self.mechanics['noah_composition'] = composition
            self.mechanics['noah_color_index'] = (self.mechanics.get('noah_color_index', 0) + 1) % 3
            self.log.append(f'{actor.name} 的【未完成構圖】進度 {composition}/3。')
            if composition >= 3:
                self.mechanics['noah_draft_charging'] = True
                self.log.append(f'{actor.name} 正在替【未完成稿】收尾；下一次行動將對全隊造成 180% 傷害！')

    def act(self, actor):
        if actor.team == 1 and actor.job == '城崎諾亞':
            self.noah_act(actor)
            return
        if actor.team == 1 and actor.job == '深淵鐘龍':
            self.clockwork_act(actor)
            return
        if actor.team == 1 and actor.job == '王城傀儡師':
            self.puppeteer_act(actor)
            return
        if actor.team == 1 and actor.job == '劍傀儡' and self.round % 2 == 0:
            self.record_skill(actor, '旋刃')
            self.log.append(f'{actor.name} 使用【旋刃】')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 0.55)
            return
        if actor.team == 1 and actor.job == '咒傀儡' and self.round % 3 == 0:
            self.record_skill(actor, '斷線咒')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'strongest'))
            self.log.append(f'{actor.name} 使用【斷線咒】')
            if target is not None and self.hit(actor, target, 0.8) and target.hp > 0:
                target.effects['break'] = self.round + 1
                self.log.append(f'{target.name} 遭到破甲，可用淨化解除。')
            return
        if actor.team == 1 and actor.job == '瘟疫縫合獸':
            if actor.hp * 2 <= actor.stats['HP'] and not self.mechanics.get('plague_phase_two'):
                self.mechanics['plague_phase_two'] = True
                self.log.append(f'{actor.name} 進入第二階段【傷口崩解】！')
            pulse = self.round % (2 if self.mechanics.get('plague_phase_two') else 3) == 0
            if pulse:
                self.record_skill(actor, '瘟疫脈衝')
                self.log.append(f'{actor.name} 使用【瘟疫脈衝】')
                for enemy in self.living(0):
                    self.add_corruption(enemy)
                return
        if actor.team == 1 and actor.job == '月影妖狐' and self.round % 3 == 0:
            self.record_skill(actor, '月影斬')
            actor.effects['moon_shadow'] = self.round + 1
            self.log.append(f'{actor.name} 使用【月影斬】：150% 單體攻擊，閃避率 +15% 至第 {self.round + 1} 回合結束。')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            if target is not None:
                self.hit(actor, target, 1.5)
            return
        if actor.team == 1 and actor.job == '血翼蝠王' and self.round % 2 == 0:
            self.record_skill(actor, '汲血撕咬')
            self.log.append(f'{actor.name} 使用【汲血撕咬】：150% 單體攻擊，吸血 30%。')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            if target is not None:
                self.hit(actor, target, 1.5, lifesteal=30)
            return
        if actor.team == 1 and actor.job == '哥布林隊長' and self.round % 3 == 0:
            self.record_skill(actor, '戰團鼓舞')
            for ally in self.living(1):
                ally.effects['bless'] = self.round + 1
                ally.effect_sources.pop('bless', None)
            self.log.append(f'{actor.name} 使用【戰團鼓舞】：存活戰團成員攻擊 +25%，持續至第 {self.round + 1} 回合結束。')
            return
        if actor.team == 1 and actor.job == '史萊姆':
            self.record_skill(actor, '彈跳撞擊')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            if target is not None:
                self.log.append(f'{actor.name} 使用【彈跳撞擊】')
                self.hit(actor, target, 0.45)
            return
        if actor.team == 1 and actor.job == '荊棘妖樹' and self.round % 3 == 0:
            self.record_skill(actor, '荊棘再生')
            healing = self.heal(actor, actor, actor.stats['HP'] // 20)
            candidates = self.living(0)
            targets = self.rng.sample(candidates, len(candidates) * 33 // 100)
            self.log.append(f'{actor.name} 使用【荊棘再生】：恢復 {healing} HP，纏繞暈眩 {len(targets)} 人。')
            for target in targets:
                target.effects['stun'] = self.round + 1
                self.log.append(f'{target.name} 暈眩，將跳過下一次行動（可淨化）。')
            return
        if actor.team == 1 and actor.job == '鐵殼魔像':
            if actor.has('charged_punch', self.round):
                self.record_skill(actor, '鐵核重拳')
                actor.effects.pop('charged_punch', None)
                target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
                if target is not None:
                    self.log.append(f'{actor.name} 使用【鐵核重拳】')
                    self.hit(actor, target, 2.5)
                return
            if self.round % 3 == 0:
                self.record_skill(actor, '蓄力')
                actor.effects['charged_punch'] = self.round + 1
                self.log.append(f'{actor.name} 使用【蓄力】：下一回合將使出 250% 倍率重拳！')
                return
        if actor.team == 1 and actor.job == '史萊姆群':
            self.record_skill(actor, '群體彈跳')
            self.log.append(f'{actor.name} 使用【群體彈跳】')
            for _ in range(3):
                target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
                if target is None:
                    break
                self.hit(actor, target, 0.45)
            return
        if actor.team == 1 and actor.job == '巨獸' and self.round % 3 == 0:
            self.record_skill(actor, '震地橫掃')
            self.log.append(f'{actor.name} 使用【震地橫掃】')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 0.75)
            return
        selected = self.select(actor)
        if not selected:
            target = self.target(actor, self.living(1 - actor.team), Rule(0, 0, True, 'always', 'lowest'), True)
            self.record_skill(actor, '普通攻擊')
            self.log.append(f'{actor.name} 使用普通攻擊')
            hit = self.hit(actor, target)
            if hit and actor.job == '毒蛛' and target.hp > 0:
                target.effects['poison'] = self.round + 2
                target.effect_sources['poison'] = actor.user_id
            if hit and actor.job == '瘟疫縫合獸' and target.hp > 0:
                self.add_corruption(target)
            return
        rule, skill, target = selected
        self.use_skill(actor, rule, skill, target)

    def use_skill(self, actor, rule, skill, target):
        """Resolve an already-selected skill.

        Automatic raids choose the skill through ``select``; interactive battle
        modes can call this method after validating a player's explicit choice.
        """
        self.record_skill(actor, skill.name)
        cooldown = max(1, skill.cooldown - actor.cooldown_reduction)
        actor.ready[rule.slot] = self.round + cooldown + 1
        self.log.append(f'{actor.name} 使用【{skill.name}】')
        effect = skill.effect
        if effect in ('group_heal', 'rally'):
            targets = self.living(actor.team) if effect == 'group_heal' else [actor]
            healing = actor.stats['治療量'] * 65 // 100 if effect == 'group_heal' else actor.stats['HP'] // 4
            for ally in targets:
                amount = self.heal(actor, ally, healing)
                self.log.append(f'{ally.name} 恢復 {amount} HP')
        elif effect in ('heal', 'greater_heal'):
            healing = actor.stats['治療量'] // 2 if actor.job == '民兵' else actor.stats['治療量']
            if effect == 'greater_heal':
                healing = healing * 180 // 100
            amount = self.heal(actor, target, healing)
            self.log.append(f'{target.name} 恢復 {amount} HP')
        elif effect == 'cleanse':
            target.effects.pop('stun', None)
            target.effects.pop('poison', None)
            target.effects.pop('break', None)
            target.status_stacks.pop('corruption', None)
            target.effect_sources.pop('poison', None)
            target.effect_sources.pop('break', None)
            self.log.append(f'移除 {target.name} 的負面狀態')
        elif effect == 'guard':
            bonus = max(1, actor.stats['HP'] // 20)
            for ally in self.living(actor.team):
                if not ally.has('guard', self.round) or ally.guard_bonus < bonus:
                    ally.guard_bonus = bonus
                    ally.effects['guard'] = self.round + 1
                    self.log.append(f'{ally.name} 防禦 +{bonus}，持續至第 {self.round + 1} 回合結束')
        elif effect in ('bless', 'stance', 'taunt'):
            target.effects[effect] = self.round + 1
            if effect == 'bless':
                target.effect_sources['bless'] = actor.user_id
            self.log.append(f'{target.name} 獲得效果，持續至第 {self.round + 1} 回合結束')
        elif effect in ('area', 'cleave'):
            for enemy in self.living(1 - actor.team):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 1.2 if effect == 'cleave' else 0.8)
        elif effect in ('double', 'triple'):
            for _ in range(3 if effect == 'triple' else 2):
                if target.hp > 0:
                    self.hit(actor, target, 0.75 if effect == 'triple' else 0.85)
        else:
            power = {'strike': 1.6, 'precise': 1.5, 'crush': 2.2, 'shield_bash': 1.2, 'poison_arrow': 1.2}.get(effect, 1)
            hit = self.hit(actor, target, power,
                           precise=effect == 'precise')
            if hit and effect == 'break' and target.hp > 0:
                target.effects['break'] = self.round + 1
                target.effect_sources['break'] = actor.user_id
            if hit and target.hp > 0 and effect in ('shield_bash', 'poison_arrow'):
                status = 'stun' if effect == 'shield_bash' else 'poison'
                if status == 'stun' and target.job == '深淵鐘龍':
                    self.log.append(f'{target.name} 免疫暈眩，鐘甲不會被盾擊直接打斷。')
                    return
                if target.job == '繪畫魔女．城崎諾亞':
                    if status == 'stun':
                        self.log.append(f'{target.name} 免疫暈眩；只有黑色能使她停止行動。')
                    else:
                        self.log.append(f'{target.name} 免疫中毒，不會受到後續毒傷。')
                    return
                if status == 'stun' and target.job == '城崎諾亞' and self.mechanics.get('noah_draft_charging'):
                    self.mechanics.update(noah_draft_charging=False, noah_composition=0, noah_color_index=0)
                    target.effects['break'] = self.round + 1
                    self.log.append(f'{target.name} 的【未完成稿】被打斷；構圖歸零並遭破甲至第 {self.round + 1} 回合結束。')
                    return
                if status == 'stun' and target.effects.pop('charged_punch', None) is not None:
                    self.log.append(f'{target.name} 的蓄力被打斷')
                target.effects[status] = max(target.effects.get(status, 0), self.round + (1 if status == 'stun' else 2))
                if status == 'poison':
                    target.effect_sources['poison'] = actor.user_id
                self.log.append(f'{target.name} {"暈眩" if status == "stun" else "中毒"}')

    def step(self):
        if self.result or self.check_end():
            return
        self.round += 1
        self.log.append(f'── 第 {self.round} 回合 ──')
        order = [f for f in self.fighters if f.hp > 0]
        self.rng.shuffle(order)  # Equal speed uses seeded random tie-breaking.
        order.sort(key=lambda f: (self.action_priority(f), f.speed), reverse=True)
        for actor in order:
            if actor.hp <= 0:
                continue
            if actor.team == 0 and actor.status_stacks.get('corruption', 0) >= 3:
                actor.status_stacks.pop('corruption', None)
                damage = max(1, actor.stats['HP'] * 12 // 100)
                actual, _, _ = self.apply_damage(actor, damage)
                self.log.append(f'{actor.name} 的【腐敗爆裂】：自身損失 {actual} HP，腐敗歸零。')
                for ally in [f for f in self.living(0) if f is not actor]:
                    splash = max(1, ally.stats['HP'] * 3 // 100)
                    taken, _, _ = self.apply_damage(ally, splash)
                    self.log.append(f'{ally.name} 受到腐敗波及，損失 {taken} HP。')
                beast = next((f for f in self.living(1) if f.job == '瘟疫縫合獸'), None)
                if beast is not None:
                    rate = 2 if self.mechanics.get('plague_phase_two') else 1.5
                    backlash = max(1, int(beast.stats['HP'] * rate / 100))
                    actual_backlash = min(beast.hp, backlash)
                    beast.hp -= actual_backlash
                    beast.combat_stats['damage_taken'] += actual_backlash
                    actor.combat_stats['damage_dealt'] += actual_backlash
                    actor.combat_stats['support_damage'] += actual_backlash
                    if actual_backlash and beast.hp == 0:
                        actor.combat_stats['knockouts'] += 1
                        beast.combat_stats['deaths'] += 1
                    self.log.append(f'{beast.name} 遭縫線逆流，損失 {actual_backlash} HP。')
                if self.check_end():
                    break
                if actor.hp == 0:
                    continue
            if actor.has('poison', self.round):
                # PvE poison only targets the opposing team: monsters poison players
                # for 5%, while player poison arrows damage monsters for 2%.
                damage = max(1, actor.stats['HP'] // (20 if actor.team == 0 else 50))
                actual_damage, partner, partner_damage = self.apply_damage(actor, damage)
                total_damage = actual_damage + partner_damage
                source = self.effect_source(actor, 'poison')
                if source is not None and source is not actor:
                    source.combat_stats['damage_dealt'] += total_damage
                    source.combat_stats['support_damage'] += total_damage
                if source is not None and source is not actor:
                    source.combat_stats['knockouts'] += int(actual_damage and actor.hp == 0)
                    if partner is not None:
                        source.combat_stats['knockouts'] += int(partner_damage and partner.hp == 0)
                self.log.append(f'{actor.name} 中毒，損失 {damage} HP')
                if self.check_end():
                    break
                if actor.hp == 0:
                    continue
            if actor.has('stun', self.round):
                actor.effects.pop('stun', None)
                self.log.append(f'{actor.name} 因暈眩跳過本次行動。')
                continue
            self.act(actor)
            if self.check_end():
                break
        if not self.result:
            for fighter in self.living(0):
                if fighter.food_regen_left and self.round >= fighter.food_regen_start:
                    amount = self.restore(
                        fighter, max(1, fighter.stats['HP'] * fighter.food_regen_permille // 1000))
                    fighter.food_regen_left -= 1
                    self.log.append(f'{fighter.name} 的【{fighter.food_name}】緩補恢復 {amount} HP。')
        if not self.result and self.round >= self.max_rounds:
            self.result = '平手（達回合上限）'



def raid_battle(participants, monster, seed):
    """Build enemies from the announcement snapshot; keep legacy raids compatible."""
    balance_version = monster.get('balance_version', 1)

    def participant_speed(participant):
        state = participant['state']
        if 'speed' in state:
            return state['speed']
        if balance_version < 3:
            return state['total'][3]
        return speed_from_equipment(state['job'], state.get('equipped', {}))

    fighters = [Fighter(p['name'], 0, p['state']['job'], dict(p['state']['combat']),
                        participant_speed(p),
                        [Rule(**r) for r in p['rules']],
                        stability=tuple(p['state'].get('stability', (100, 100))),
                        lifesteal=p['state'].get('lifesteal', 0),
                        damage_guard_chance=p['state'].get('damage_guard_chance', 0),
                        vulnerable_chance=p['state'].get('vulnerable_chance', 0),
                        vulnerable_percent=p['state'].get('vulnerable_percent', 0),
                        healing_share=p['state'].get('healing_share', 0),
                        armed=bool(p['state'].get('equipped', {}).get('武器')),
                        user_id=p.get('id')) for p in participants]
    badge_logs = []
    for fighter, participant in zip(fighters, participants):
        if any(ITEMS[key].party_bonus for key in participant['state'].get('equipped', {}).values() if key in ITEMS):
            count = min(5, (len(participants) + 1) // 2)
            total = participant['state']['total']
            before = combat_from_stats(total)
            after = combat_from_stats([value + count for value in total])
            for stat in before:
                fighter.stats[stat] += after[stat] - before[stat]
            fighter.hp = fighter.stats['HP']
            badge_logs.append(f'{fighter.name} 的【戰團徽章】生效：{len(participants)} 人參戰，生命力／力氣／耐力／靈巧／信仰各 +{count}，整場固定。')
    provision_logs = []
    stat_caps = {'命中率': 150, '閃避率': 40, '暴擊率': 50}
    for fighter, participant in zip(fighters, participants):
        provisions = participant.get('provisions', {})
        food = provisions.get('food')
        if food:
            fighter.food_name = food['name']
            fighter.food_heal_permille = food['heal_permille']
            fighter.food_regen_permille = food['regen_permille']
            fighter.food_regen_rounds = food['regen_rounds']
            provision_logs.append(f'{fighter.name} 攜帶【{food["name"]}】：生命低於 40% 時自動食用。')
        potion = provisions.get('potion')
        if potion:
            stat, amount = potion['stat'], potion['amount']
            before = fighter.stats[stat]
            if potion['mode'] == 'percent':
                fighter.stats[stat] = max(1 if stat == 'HP' else 0, before * (100 + amount) // 100)
            else:
                fighter.stats[stat] = min(stat_caps.get(stat, 10_000), before + amount)
            if stat == 'HP':
                fighter.hp = fighter.stats['HP']
            provision_logs.append(
                f'{fighter.name} 使用【{potion["name"]}】：{stat} {before} → {fighter.stats[stat]}，整場固定。')
    fortune_logs = []
    fortune_rng = random.Random(f'{seed}:divination')

    def percent_stat(fighter, stat, percent):
        before = fighter.stats[stat]
        fighter.stats[stat] = max(1 if stat == 'HP' else 0, before * (100 + percent) // 100)
        if stat == 'HP':
            fighter.hp = fighter.stats['HP']

    for fighter, participant in zip(fighters, participants):
        fortune = participant.get('fortune') or {}
        card = fortune.get('id')
        if not card:
            continue
        fighter.fortune_card = card
        if card == 'strength':
            percent_stat(fighter, '攻擊', 6)
        elif card == 'emperor':
            percent_stat(fighter, '防禦', 6)
        elif card == 'empress':
            percent_stat(fighter, 'HP', 6)
        elif card == 'star':
            percent_stat(fighter, '治療量', 8)
            fighter.healing_received_percent = 5
        elif card == 'chariot':
            fighter.speed += 8
            fighter.stats['命中率'] = min(150, fighter.stats['命中率'] + 3)
        elif card == 'moon':
            fighter.stats['閃避率'] = min(40, fighter.stats['閃避率'] + 4)
        elif card == 'sun':
            fighter.stability = (min(fighter.stability[1], fighter.stability[0] + 8), fighter.stability[1])
        elif card == 'justice':
            fighter.damage_dealt_percent = 6
            fighter.damage_taken_percent = 4
        elif card == 'hanged_man':
            fighter.speed = max(1, fighter.speed - 10)
            percent_stat(fighter, '防禦', 10)
        elif card == 'devil':
            percent_stat(fighter, '攻擊', 10)
            percent_stat(fighter, '治療量', 10)
            fighter.healing_received_percent = -20
        elif card == 'tower':
            percent_stat(fighter, 'HP', -10)
            percent_stat(fighter, '攻擊', 12)
            fighter.stats['暴擊率'] = min(50, fighter.stats['暴擊率'] + 5)
        elif card == 'death':
            fighter.lifesteal += 5
        elif card == 'magician':
            fighter.cooldown_reduction = 1
        elif card == 'fool':
            stats = ['HP', '攻擊', '防禦', '治療量', '速度', '命中率', '閃避率', '暴擊率']
            raised = fortune_rng.sample(stats, 2)
            lowered = fortune_rng.choice([stat for stat in stats if stat not in raised])
            for stat in raised:
                if stat == '速度':
                    fighter.speed = max(1, fighter.speed * 110 // 100)
                else:
                    percent_stat(fighter, stat, 10)
            if lowered == '速度':
                fighter.speed = max(1, fighter.speed * 90 // 100)
            else:
                percent_stat(fighter, lowered, -10)
            fortune['fool_result'] = dict(raised=raised, lowered=lowered)
        elif card == 'world':
            for stat in ('HP', '攻擊', '防禦', '治療量', '命中率', '閃避率', '暴擊率'):
                percent_stat(fighter, stat, 10)
            fighter.speed = max(1, fighter.speed * 110 // 100)
        for stat, cap in stat_caps.items():
            fighter.stats[stat] = min(cap, fighter.stats[stat])
        fortune_logs.append(f'{fighter.name} 的占卜【{fortune.get("name", card)}】生效；本場結算經驗 +10%。')

    # Lovers form exclusive two-person links.  A linked teammate cannot be
    # selected by another Lovers card, keeping damage sharing non-recursive.
    linked = set()
    for fighter in fighters[:len(participants)]:
        if fighter.fortune_card != 'lovers' or fighter.user_id in linked:
            continue
        candidates = [ally for ally in fighters[:len(participants)]
                      if ally is not fighter and ally.user_id not in linked]
        if not candidates:
            fortune_logs.append(f'{fighter.name} 的【戀人】找不到可以締結連結的隊友。')
            continue
        partner = fortune_rng.choice(candidates)
        fighter.linked_user_id = partner.user_id
        partner.linked_user_id = fighter.user_id
        linked.update((fighter.user_id, partner.user_id))
        fortune_logs.append(f'{fighter.name} 的【戀人】與 {partner.name} 締結連結，共享受到的傷害。')
    participant_average = sum(p['state']['level'] for p in participants) / len(participants)
    average = participant_average
    profile = monster.get('profile')
    party_hp_scale = 1
    if balance_version >= 3 and monster.get('tier') in REFERENCE_LEVELS:
        # V3 encounters have an explicit content level.  Quality raises that
        # level instead of multiplying already-derived stats, while party size
        # remains the only automatic HP scaling axis.
        average = REFERENCE_LEVELS[monster['tier']] + profile.get('level_bonus', 0)
        base_hp = 100 + average * 19
        party_hp_scale = len(participants)
    elif balance_version >= 2 and monster.get('tier') in REFERENCE_LEVELS:
        average = REFERENCE_LEVELS[monster['tier']]
        base_hp = len(participants) * (150 + average * 28)
    else:
        # Announced V1 encounters retain their player-level scaling.
        base_hp = sum(150 + p['state']['level'] * 28 for p in participants)
    base_attack = 22 + average * 6 + max(0, average - 20)
    # Shorter encounters need each successful monster action to stay relevant.
    # V3's 10% threat budget is applied before per-monster skill/profile values.
    if balance_version >= 3 and monster.get('tier') in REFERENCE_LEVELS:
        base_attack *= 1.1
    stats = {'HP': int(base_hp),
             '攻擊': int(base_attack),
             '防禦': int(10 + average * 2),
             '治療量': 0, '命中率': 92, '閃避率': 5, '暴擊率': 10}
    # V1/V2 announcement snapshots stored a multiplier. V3 stores the new
    # absolute narrow-scale speed, so old announced raids keep their ordering.
    speed = 10 if balance_version >= 3 else int(12 + average * 2)
    if profile:
        for stat, key in (('HP', 'hp'), ('攻擊', 'attack'), ('防禦', 'defense')):
            stats[stat] = max(1, int(stats[stat] * Decimal(str(profile[key]))))
        stats['HP'] *= party_hp_scale
        stats.update(命中率=profile['hit'], 閃避率=profile['dodge'], 暴擊率=profile['crit'])
        speed = max(1, int(profile['speed'] if balance_version >= 3 else speed * profile['speed']))
    elif monster['kind'] == '鐵殼魔像':
        stats['HP'] = int(stats['HP'] * 1.2)
        stats['防禦'] *= 2
    if balance_version >= 2:
        # V2 softened monster scaling toward the actual party level.  V3 uses
        # the fixed tier + quality content level so over-levelled players are
        # genuinely stronger when revisiting an encounter.
        if balance_version == 2 and monster.get('tier') in REFERENCE_LEVELS:
            reference = REFERENCE_LEVELS[monster['tier']]
            level_delta = Decimal(str(participant_average)) / Decimal(reference) - Decimal(1)
            level_hp = max(Decimal('0.6'), min(Decimal('2'), Decimal(1) + level_delta * Decimal('0.4')))
            level_attack = max(Decimal('0.85'), min(Decimal('1.25'),
                               Decimal(1) + level_delta * Decimal('0.1')))
            stats['HP'] = max(1, int(stats['HP'] * level_hp))
            stats['攻擊'] = max(1, int(stats['攻擊'] * level_attack))
        manual = monster.get('manual_strength', monster.get('strength', 1))
        difficulty = monster.get('difficulty_multiplier', 1)
        dynamic = {'HP': difficulty, '攻擊': 1 + (difficulty - 1) * 0.4,
                   '防禦': 1 + (difficulty - 1) * 0.1}
        for stat in ('HP', '攻擊', '防禦'):
            stats[stat] = max(1, int(stats[stat] * manual * dynamic[stat]))
    else:
        for stat in ('HP', '攻擊', '防禦'):
            stats[stat] = max(1, int(stats[stat] * monster.get('strength', 1)))
    count = profile['count'] if profile else 1
    for i in range(count):
        individual = dict(stats)
        if monster['kind'] == '王城傀儡師':
            shares = (50, 25, 25)
            individual['HP'] = max(1, stats['HP'] * shares[i] // 100
                                   + (stats['HP'] - sum(stats['HP'] * share // 100 for share in shares) if i == 0 else 0))
        else:
            individual['HP'] = max(1, stats['HP'] // count + (i < stats['HP'] % count))
        name = monster_name(monster) + (f'・{i + 1}' if count > 1 else '')
        job = '史萊姆' if count > 1 and monster['kind'] == '史萊姆群' else monster['kind']
        if monster['kind'] == '哥布林戰團':
            job = '哥布林隊長' if i == 0 else '哥布林打手'
            name = monster_name(monster) + ('・隊長' if i == 0 else f'・打手{i}')
        if monster['kind'] == '王城傀儡師':
            job = ('王城傀儡師', '劍傀儡', '咒傀儡')[i]
            name = monster_name(monster) if i == 0 else f'{monster_name(monster)}・{job}'
            individual['攻擊'] = max(1, individual['攻擊'] * (100, 60, 40)[i] // 100)
            individual['防禦'] = max(1, individual['防禦'] * (100, 120, 80)[i] // 100)
        fighters.append(Fighter(name, 1, job, individual, speed, []))
    battle = Battle(fighters, seed=seed)
    if monster['kind'] == '深淵鐘龍':
        battle.mechanics['next_clock_charge'] = 3
    if monster['kind'] == '王城傀儡師':
        battle.mechanics['puppet_base_attack'] = fighters[-3].stats['攻擊']
        fighters[-2].effects['taunt'] = battle.max_rounds
    if monster['kind'] == '城崎諾亞':
        colors = ('red', 'yellow', 'blue')
        color = monster.get('primary_color') or colors[random.Random(seed).randrange(3)]
        battle.mechanics.update(noah_primary_color=color, noah_color_index=0,
                                noah_composition=0, noah_draft_charging=False)
    battle.log.extend(badge_logs)
    battle.log.extend(provision_logs)
    battle.log.extend(fortune_logs)
    return battle


def dump_battle(battle):
    from dataclasses import asdict
    return dict(fighters=[asdict(f) for f in battle.fighters], round=battle.round,
                max_rounds=battle.max_rounds, result=battle.result, log=battle.log,
                random_state=battle.rng.getstate(), mechanics=battle.mechanics)


def load_battle(data):
    def tuples(value):
        return tuple(tuples(v) for v in value) if isinstance(value, (tuple, list)) else value
    fighters = []
    for data_f in data['fighters']:
        f = Fighter(data_f['name'], data_f['team'], data_f['job'], data_f['stats'],
                    data_f.get('speed', data_f.get('dexterity', 10)),
                    [Rule(**r) for r in data_f['rules']])
        f.hp = data_f['hp']
        f.ready = {int(k): v for k, v in data_f['ready'].items()}
        f.effects = data_f['effects']
        f.effect_sources = data_f.get('effect_sources', {})
        f.guard_bonus = data_f.get('guard_bonus', 0)
        f.stability = tuple(data_f.get('stability', (100, 100)))
        f.armed = data_f.get('armed', True)
        f.lifesteal = data_f.get('lifesteal', 0)
        f.damage_guard_chance = data_f.get('damage_guard_chance', 0)
        f.vulnerable_chance = data_f.get('vulnerable_chance', 0)
        f.vulnerable_percent = data_f.get('vulnerable_percent', 0)
        f.healing_share = data_f.get('healing_share', 0)
        f.status_stacks = data_f.get('status_stacks', {})
        f.user_id = data_f.get('user_id')
        f.food_name = data_f.get('food_name', '')
        f.food_heal_permille = data_f.get('food_heal_permille', 0)
        f.food_regen_permille = data_f.get('food_regen_permille', 0)
        f.food_regen_rounds = data_f.get('food_regen_rounds', 0)
        f.food_used = data_f.get('food_used', False)
        f.food_regen_left = data_f.get('food_regen_left', 0)
        f.food_regen_start = data_f.get('food_regen_start', 0)
        f.fortune_card = data_f.get('fortune_card', '')
        f.damage_dealt_percent = data_f.get('damage_dealt_percent', 0)
        f.damage_taken_percent = data_f.get('damage_taken_percent', 0)
        f.healing_received_percent = data_f.get('healing_received_percent', 0)
        f.cooldown_reduction = data_f.get('cooldown_reduction', 0)
        f.linked_user_id = data_f.get('linked_user_id')
        saved_stats = data_f.get('combat_stats', {})
        f.combat_stats = empty_combat_stats()
        for key in f.combat_stats:
            if key in saved_stats:
                f.combat_stats[key] = saved_stats[key]
        if 'direct_damage' not in saved_stats:
            # Old snapshots cannot distinguish direct hits from indirect poison damage.
            f.combat_stats['direct_damage'] = saved_stats.get('damage_dealt', 0)
        fighters.append(f)
    battle = Battle(fighters, max_rounds=data['max_rounds'])
    battle.round, battle.result, battle.log = data['round'], data['result'], list(data['log'])
    battle.mechanics = dict(data.get('mechanics', {}))
    battle.rng.setstate(tuples(data['random_state']))
    return battle
