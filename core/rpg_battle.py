"""Deterministic when seeded: automatic turn combat and persistent skill tactics."""
from dataclasses import dataclass, field
from decimal import Decimal
import random

from core.rpg_character import CharacterError, ITEMS, combat_from_stats, speed_from_equipment
from core.rpg_monsters import REFERENCE_LEVELS, monster_name
from core.rpg import level_for
from core import rpg_maze_traits as maze_traits


CONDITIONS = {'always': '可用就施放', 'self40': '自身 HP ≤ 指定比例',
              'ally50': '隊伍有人 HP ≤ 指定比例', 'allies_injured': '受傷隊友數量 ≥ 指定人數',
              'enemies3': '存活敵人數量 ≥ 指定數量',
              'enemy_hp_lte': '任一敵人 HP ≤ 指定比例',
              'enemy_hp_gte': '任一敵人 HP ≥ 指定比例',
              'round_gte': '戰鬥回合 ≥ 指定回合',
              'ally_debuff': '隊友有可淨化負面狀態（含自己）',
              'enemy_charging': '敵人正在蓄力',
              'enemy_guard': '敵人有可擊破防護',
              'enemy_broken': '敵人處於破甲',
              'enemy_add': '非首領敵人存活',
              'ally_debuff_stacks': '隊友疊層負面狀態 ≥ 指定層數',
              'mechanic_target': '機制目標出現'}
CONDITION_LIMITS = {
    'self40': (1, 100, 40, '%'),
    'ally50': (1, 100, 50, '%'),
    'allies_injured': (1, 20, 2, ' 人'),
    'enemies3': (1, 3, 3, ' 隻'),
    'enemy_hp_lte': (1, 100, 30, '%'),
    'enemy_hp_gte': (1, 100, 70, '%'),
    'round_gte': (1, 30, 5, ' 回合'),
    'ally_debuff_stacks': (1, 10, 2, ' 層'),
}
TARGETS = {'lowest': '血量比例最低', 'strongest': '攻擊最高', 'self': '自己',
           'debuffed': '有可淨化負面狀態的隊友',
           'highest_hp': '血量比例最高', 'boss': '首領本體優先',
           'add': '非首領敵人優先', 'mechanic': '機制目標優先'}
OFFENSIVE_TARGETS = {'boss', 'add', 'mechanic'}
BASIC_TARGETS = {key: label for key, label in TARGETS.items() if key not in ('self', 'debuffed')}


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


@dataclass(frozen=True)
class Passive:
    id: int
    name: str
    description: str


PASSIVES = {
    '裝甲步兵': (
        Passive(1, '百鍊連式', '不同傷害技能連續使用可累積連式；重複技能重置為 1，普攻不影響。第三式傷害 +50%，命中後破甲至下一回合結束並歸零。'),
        Passive(2, '攻守輪轉', '準備技能獲得守勢、傷害技能獲得攻勢，各最多 2 層。傷害技能每層守勢傷害 +15%；準備技能每層攻勢恢復 3% 最大 HP。'),
        Passive(3, '浴血戰意', '每回合首次受到直接傷害獲得 2 層血怒，攻守架勢再獲得 1 層，最多 5。三層以上使下一個傷害技能 +30%；滿五層另有 15% 吸血。'),
    ),
    '騎士': (
        Passive(1, '復仇誓約', '挑釁期間每回合首次受擊獲得 1 層復仇，最多 3。下一個主動傷害技能每層傷害 +12%，並恢復 2% 最大 HP。'),
        Passive(2, '守望誓約', '護衛期間其他隊友每回合首次受擊或免疫負面狀態時獲得守望，最多 2。滿層護衛使全隊恢復騎士 2% 最大 HP，並獲得 10% 減傷。'),
        Passive(3, '槍盾連攜', '盾擊命中留下破綻，使下一次騎士衝鋒傷害 +25%；衝鋒命中獲得衝勢，使下一次盾擊追加一次 40% 傷害。'),
    ),
    '弓兵': (
        Passive(1, '無間箭勢', '每次直接命中獲得箭勢，未命中歸零，最多 6。每層使下一次命中傷害 +3%；六層後再次命中會追加一支 80% 傷害箭並歸零。'),
        Passive(2, '猛毒調律', '自身毒箭侵蝕每次傷害使目標獲得 1 層毒性，最多 3。直接命中滿層目標時，引爆 180% 攻擊的無視防禦傷害；不移除侵蝕。'),
        Passive(3, '弱點觀測', '每次直接暴擊獲得 1 層洞察，最多 3。滿層後下一次傷害行動必定命中且所有直接傷害 +40%；強化期間不再累積洞察。'),
    ),
    '僧侶': (
        Passive(1, '恩典回響', '有效治療技能獲得 1 層恩典，最多 3。滿層後下一次有效治療使主要目標額外恢復 20%，並使最多三名受傷隊友各恢復 15%。'),
        Passive(2, '三重聖歌', '有效治療、祝福、淨化各提供一種樂章。集齊後全隊恢復 10% 治療量，下一次傷害行動 +10%；每場最多三次，聖光不提供治療樂章。'),
        Passive(3, '光明輪轉', '傷害獲得輝光、治療獲得戒律，各最多 3。治療每層輝光 +15%，傷害每層戒律 +15%；聖光同時消耗兩者但不產生新層數。'),
    ),
}

DAMAGE_EFFECTS = {'strike', 'break', 'cleave', 'crush', 'knight_charge', 'shield_bash',
                  'double', 'triple', 'area', 'hindering_shot', 'poison_arrow', 'holy_light'}
HEALING_EFFECTS = {'heal', 'group_heal', 'holy_light'}
HYMN_HEALING_EFFECTS = {'heal', 'group_heal'}


def passive_description(passive):
    return passive.description


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
    '騎士': (Skill('挑釁反擊', 'taunt', 3, '吸引敵方單體攻擊；受到直接攻擊後對攻擊者造成 100% 傷害，持續至下一回合結束',
                   timing=PREPARATION_TIMING),
             Skill('護衛', 'guard', 3, '全隊防禦增加施放者自身防禦的 100%，並免疫可淨化負面狀態，持續至下一回合結束', 'ally50',
                   timing=PREPARATION_TIMING),
             Skill('騎士衝鋒', 'knight_charge', 3, '以自身最大 HP 造成 50% 單體傷害'),
             Skill('盾擊', 'shield_bash', 4, '造成 120% 傷害，命中後打斷蓄力並暈眩至下一回合結束（跳過一次行動）'),
             Skill('重整旗鼓', 'rally', 4, '恢復自身最大 HP 的 50%', 'self40')),
    '弓兵': (Skill('連射', 'double', 2, '兩次 90% 傷害，各自判定命中'),
             Skill('妨害射擊', 'hindering_shot', 3, '造成 120% 傷害，命中後使敵方攻擊降低 20%，持續至下一回合結束'),
             Skill('箭雨', 'area', 4, '對所有敵人各造成三次 40% 傷害', 'enemies3'),
             Skill('三連矢', 'triple', 4, '對單一敵人連射三次，每次 85% 傷害，分別判定命中'),
             Skill('毒箭', 'poison_arrow', 3, '造成 110% 傷害；命中後於後續兩回合各造成 70% 攻擊的無視防禦傷害，每支毒箭分開計算')),
    '僧侶': (Skill('治療', 'heal', 2, '恢復一名隊友生命', 'ally50'),
             Skill('祝福', 'bless', 3, '提升一名隊友攻擊 25%，持續至下一回合結束',
                   timing=PREPARATION_TIMING),
             Skill('淨化', 'cleanse', 2, '移除一名隊友的中毒、毒箭侵蝕、破甲、暈眩、虛弱與腐敗', 'ally_debuff'),
             Skill('群體治療', 'group_heal', 4, '恢復全體存活隊友各 65% 治療量的 HP', 'ally50'),
             Skill('聖光', 'holy_light', 4, '對全體敵人造成 90% 傷害，並恢復一名隊友 70% 治療量的 HP', 'ally50')),
}
ALLY_EFFECTS = {'heal', 'guard', 'bless', 'cleanse', 'group_heal', 'holy_light'}
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
        'ally_debuff_stacks': f'隊友疊層負面狀態 ≥ {threshold} 層',
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
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_passives (
                guild_id INTEGER, user_id INTEGER, job TEXT, passive_id INTEGER,
                PRIMARY KEY (guild_id, user_id, job))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_basic_targets (
                guild_id INTEGER, user_id INTEGER, job TEXT, target TEXT,
                PRIMARY KEY (guild_id, user_id, job))''')

    def basic_target(self, guild, user, job):
        row = self.db.execute(
            'SELECT target FROM rpg_basic_targets WHERE guild_id=? AND user_id=? AND job=?',
            (guild, user, job)).fetchone()
        return row[0] if row and row[0] in BASIC_TARGETS else 'lowest'

    def configure_basic_target(self, guild, user, job, target):
        if job not in SKILLS or target not in BASIC_TARGETS:
            raise CharacterError('無效的普通攻擊目標。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rpg_basic_targets VALUES (?,?,?,?)',
                            (guild, user, job, target))

    def available(self, guild, user, job):
        return unlocked_skills(job, level_for(self.store.xp(guild, user)))

    def available_passives(self, guild, user, job):
        if job not in PASSIVES or level_for(self.store.xp(guild, user)) < 50:
            return ()
        return PASSIVES[job]

    def passive(self, guild, user, job):
        available = self.available_passives(guild, user, job)
        if not available:
            return None
        row = self.db.execute(
            'SELECT passive_id FROM rpg_passives WHERE guild_id=? AND user_id=? AND job=?',
            (guild, user, job)).fetchone()
        if row is None:
            return None
        return next((passive for passive in available if passive.id == row[0]), None)

    def equip_passive(self, guild, user, job, passive_id):
        available = self.available_passives(guild, user, job)
        if not available:
            raise CharacterError('職業被動技能需要 Lv.50 晉升後解鎖。')
        if type(passive_id) is not int or passive_id not in {passive.id for passive in available}:
            raise CharacterError('無效的被動技能。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rpg_passives VALUES (?, ?, ?, ?)',
                            (guild, user, job, passive_id))
        return next(passive for passive in available if passive.id == passive_id)

    def rules(self, guild, user, job):
        saved = {row[0]: Rule(row[0], row[1], bool(row[2]), row[3], row[4], row[5],
                              condition_value(row[3], row[6])) for row in self.db.execute(
            'SELECT slot, priority, enabled, condition, target, skill_id, condition_value FROM rpg_tactics '
            'WHERE guild_id=? AND user_id=? AND job=?', (guild, user, job))}
        # Skill ID 3 used to be the self-targeted low-HP stance 堅守. Convert only
        # that exact legacy default, in whichever slot it was equipped, so custom
        # conditions on the new attack remain intact.
        if job == '騎士':
            for slot, legacy in list(saved.items()):
                skill_id = legacy.skill_id or legacy.slot
                if (skill_id == 3 and legacy.condition == 'self40'
                        and legacy.target == 'self'):
                    saved[slot] = Rule(legacy.slot, legacy.priority, legacy.enabled,
                                       'always', 'lowest', legacy.skill_id, None)
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
        if target in OFFENSIVE_TARGETS and skill.effect in ALLY_EFFECTS:
            raise CharacterError('這個目標規則僅供攻擊技能使用。')
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
    alternating_damage_percent: int = 0
    defense_conversion: bool = False
    stored_defense_attack: int = 0
    critical_damage_percent: int = 150
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
    first_skill_cooldown_reduction: int = 0
    first_skill_cooldown_used: bool = False
    linked_user_id: int | None = None
    passive_id: int | None = None
    passive_state: dict = field(default_factory=dict)
    is_boss: bool = False
    mechanic_priority: int = 0
    basic_target: str = 'lowest'

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
        self._passive_action = None

    @staticmethod
    def record_skill(actor, name):
        used = actor.combat_stats['skills_used']
        used[name] = used.get(name, 0) + 1

    def accuracy_bonus(self, actor):
        return 0

    def hit_chance(self, actor, target):
        """Resolve accuracy and evasion ratings into a percentage chance."""
        evasion = target.stats['閃避率'] + (15 if target.has('moon_shadow', self.round) else 0)
        return max(10, actor.stats['命中率'] + self.accuracy_bonus(actor) - evasion)

    def critical_bonus(self, actor):
        calibration = actor.status_stacks.get('crystal_heart_calibration', 0)
        return actor.passive_state.get('crystal_calibration', 0) * calibration

    def critical_chance(self, actor):
        return max(0, min(100, actor.stats['暴擊率'] + self.critical_bonus(actor)))

    def damage_dealt_multiplier(self, actor):
        return (100 + actor.damage_dealt_percent) / 100

    @staticmethod
    def direct_damage_multiplier(actor):
        return (100 + actor.status_stacks.get('maze_direct_damage_percent', 0)) / 100

    def debuff_damage_multiplier(self, actor, target):
        """Optional encounter bonus against targets carrying a cleanseable debuff."""
        percent = actor.status_stacks.get('maze_violet_percent', 0)
        if not percent:
            return 1.0
        affected = (any(target.has(effect, self.round)
                        for effect in ('poison', 'break', 'stun', 'weak', 'vulnerable'))
                    or target.status_stacks.get('corruption', 0)
                    or target.status_stacks.get('drowning_mark', 0)
                    or target.status_stacks.get('poison_arrows'))
        return (100 + percent) / 100 if affected else 1.0

    def crystal_damage_multiplier(self, actor, target):
        percent = 0
        if target.has('poison', self.round) or target.status_stacks.get('poison_arrows'):
            percent += actor.status_stacks.get('crystal_poison_damage_percent', 0)
        if target.has('break', 10**9):
            percent += actor.status_stacks.get('crystal_break_damage_percent', 0)
        if target.hp * 100 <= target.stats['HP'] * 30:
            percent += actor.status_stacks.get('crystal_low_enemy_damage_percent', 0)
        if actor.hp * 100 >= actor.stats['HP'] * 80:
            percent += actor.status_stacks.get('crystal_high_hp_damage_percent', 0)
        return (100 + percent) / 100

    def _maze_final_hit(self, actor, target, target_hp_before, actual_damage):
        if not actual_damage or not self.mechanics.get('maze_final'):
            return
        contracts = self.mechanics.get('maze_final_contracts', {})
        boss = next((fighter for fighter in self.living(1) if fighter.is_boss), None)
        if actor.team == 0 and target.team == 1 and target.is_boss:
            azure = contracts.get('azure', 0)
            if target.hp > 0:
                triggered = self.mechanics.setdefault('maze_final_phases', [])
                for threshold in (70, 35):
                    if (threshold not in triggered
                            and target_hp_before * 100 > target.stats['HP'] * threshold
                            and target.hp * 100 <= target.stats['HP'] * threshold):
                        triggered.append(threshold)
                        if azure:
                            shield = max(1, target.stats['HP'] * azure * 3 // 100)
                            self.mechanics['maze_final_shield'] = (
                                self.mechanics.get('maze_final_shield', 0) + shield)
                            self.log.append(
                                f'{target.name} 在 {threshold}% 構圖轉換時展開畫幕護盾 {shield}。')
                        if threshold == 70:
                            self.clear_negative_effects(target)
                            self.mechanics['maze_final_phase'] = 2
                            self.log.append(f'{target.name} 進入【源色改寫】，洗去短期負面狀態。')
                            if self.mechanics.get('maze_final_route') == 'noah':
                                stats = dict(target.stats)
                                stats.update(HP=max(1, target.stats['HP'] * 10 // 100),
                                             攻擊=max(1, target.stats['攻擊'] * 45 // 100),
                                             防禦=max(1, target.stats['防禦'] * 55 // 100),
                                             暴擊率=0)
                                paint = Fighter('未乾色塊', 1, '未乾色塊', stats, 35, [],
                                                is_boss=False, mechanic_priority=2)
                                paint.effects['mechanic_target'] = self.round + 3
                                self.fighters.append(paint)
                                self.mechanics['maze_wet_paint_round'] = self.round
                                self.log.append('【未乾色塊】出現；若不及時擊倒將強化色彩技能。')
                        elif threshold == 35:
                            self.mechanics.update(maze_final_phase=3,
                                                  noah_draft_charging=False,
                                                  noah_final_charging=False,
                                                  noah_final_cycle=0)
                            for add in self.living(1):
                                if add.job == '未乾色塊':
                                    add.hp = 0
                            self.log.append(f'{target.name} 進入【完成畫作】：最後一筆已經展開。')
            crimson = contracts.get('crimson', 0)
            if crimson and actor.hp > 0 and target.hp > 0:
                hits = self.mechanics.get('maze_final_direct_hits', 0) + 1
                threshold = max(5, 11 - crimson * 2)
                if hits >= threshold:
                    hits = 0
                    retaliation = max(1, target.stats['攻擊'] * (30 + crimson * 8) // 100)
                    taken, _, _ = self.apply_damage(actor, retaliation)
                    self.log.append(f'{target.name} 以【緋紅回筆】反擊 {actor.name}，造成 {taken} 傷害。')
                self.mechanics['maze_final_direct_hits'] = hits
        elif actor.team == 1 and actor.is_boss and target.team == 0:
            violet = contracts.get('violet', 0)
            if violet and target.hp > 0 and not target.has('immunity', self.round):
                before = target.status_stacks.get('corruption', 0)
                target.status_stacks['corruption'] = min(3, before + 1)
                self.log.append(
                    f'{target.name} 受到紫蝕侵染，腐敗變為 {target.status_stacks["corruption"]}/3 層。')

    def _maze_party_round_end(self):
        if self.result or self.round % 3:
            return
        for fighter in self.living(0):
            percent = fighter.status_stacks.get('maze_renewal_percent', 0)
            if percent:
                amount = self.restore(fighter, fighter.stats['HP'] * percent / 100
                                      * self.healing_received_multiplier(fighter))
                if amount:
                    self.log.append(f'{fighter.name} 的【回春契約】恢復 {amount} HP。')

    def _maze_final_round_end(self):
        if not self.mechanics.get('maze_final') or self.result:
            return
        contracts = self.mechanics.get('maze_final_contracts', {})
        boss = next((fighter for fighter in self.living(1) if fighter.is_boss), None)
        if boss is None:
            return
        verdant = contracts.get('verdant', 0)
        if verdant and self.round % 3 == 0:
            healing = boss.stats['HP'] * verdant // 200
            if boss.has('break', self.round):
                healing //= 2
            amount = self.restore(boss, healing)
            if amount:
                self.log.append(f'{boss.name} 的【翠綠回生】恢復 {amount} HP。')
        if contracts.get('black', 0) >= 3 and self.round % 5 == 0 and self.living(0):
            self.log.append(f'{boss.name} 的【極黑共鳴】追加一次行動！')
            self.act(boss)
            self.check_end()
        wet_round = self.mechanics.get('maze_wet_paint_round')
        wet = next((fighter for fighter in self.living(1) if fighter.job == '未乾色塊'), None)
        if wet is not None and wet_round is not None and self.round >= wet_round + 2:
            boss.damage_dealt_percent += 10
            wet.hp = 0
            self.mechanics.pop('maze_wet_paint_round', None)
            self.log.append(f'{boss.name} 吸收【未乾色塊】，後續傷害 +10%。')

    def damage_taken_multiplier(self, target):
        percent = target.damage_taken_percent
        percent -= (target.passive_state.get('crystal_wall', 0)
                    * target.status_stacks.get('crystal_immovable_wall', 0))
        if target.hp * 100 <= target.stats['HP'] * 30:
            percent -= target.status_stacks.get('crystal_low_hp_reduction_percent', 0)
        if target.has('fortune_hermit_guard', self.round):
            percent -= 25
        return max(0, 100 + percent) / 100

    def healing_done_multiplier(self, actor):
        return 1.0

    def healing_received_multiplier(self, target):
        return max(0, 100 + target.healing_received_percent) / 100 * maze_traits.healing_multiplier(target, self.round)

    @staticmethod
    def passive(actor, job, passive_id):
        return actor.team == 0 and actor.job == job and actor.passive_id == passive_id

    def _begin_passive_action(self, actor, skill=None, target=None, basic=False):
        effect = skill.effect if skill is not None else None
        damaging = basic or effect in DAMAGE_EFFECTS
        context = dict(actor=actor, skill=skill, target=target, basic=basic, damaging=damaging,
                       multiplier=1.0, force_hit=False, hits=0, actual_damage=0, heals=[],
                       critical_hits=0,
                       cleansed=0, hymn_bless=False, suppress_insight=False,
                       chain_broken=set())
        state = actor.passive_state

        def crystal(effect_name):
            return actor.status_stacks.get(f'crystal_{effect_name}', 0)

        # Source crystals are intentionally kept in passive_state: each raid_battle
        # creates fresh fighters, so their stacks reset for every painting.
        if damaging:
            value = crystal('tempered_embers')
            if value and skill is not None:
                last = state.get('crystal_embers_last')
                stacks = state.get('crystal_embers', 0)
                stacks = min(5, stacks + 1) if last != effect else max(0, stacks - 2)
                state.update(crystal_embers_last=effect, crystal_embers=stacks)
                context['multiplier'] *= 1 + stacks * value / 100
                if stacks == 5:
                    self.log.append(f'{actor.name} 的【百鍊餘火】達到 5 層。')
            value = crystal('formation_breaker')
            if value and target is not None and target.has('break', self.round):
                stacks = min(5, state.get('crystal_formation', 0) + 1)
                state['crystal_formation'] = stacks
                context['multiplier'] *= 1 + stacks * value / 100
            value = crystal('blooded_blade')
            stacks = state.pop('crystal_blooded', 0) if value else 0
            if stacks:
                context['multiplier'] *= 1 + stacks * value / 100
                self.log.append(f'{actor.name} 消耗 {stacks} 層【浴血鋒刃】。')
            value = crystal('endless_offense')
            if value and state.pop('crystal_offense_ready', False):
                context['multiplier'] *= 1 + value / 100
            value = crystal('heavy_suppression')
            if value:
                context['multiplier'] *= 1 + max(1, actor.hp * 4 // actor.stats['HP']) * value / 100
            value = crystal('vengeance_mark')
            stacks = state.pop('crystal_vengeance', 0) if value else 0
            if stacks:
                context['multiplier'] *= 1 + stacks * value / 100
                self.log.append(f'{actor.name} 消耗 {stacks} 層【復仇刻痕】。')
            for source, required in (('guardian_oath', 'shield_bash'),
                                     ('steel_echo', 'knight_charge'),
                                     ('life_lance', 'knight_charge')):
                value = crystal(source)
                stacks = state.pop(f'crystal_{source}_stacks', 0) if value and effect == required else 0
                if stacks:
                    context['multiplier'] *= 1 + stacks * value / 100
                    self.log.append(f'{actor.name} 消耗 {stacks} 層源色刻印。')
            for source, state_key in (('endless_arrow', 'crystal_arrows'),
                                      ('focused_shot', 'crystal_focus'),
                                      ('venom_amplifier', 'crystal_venom')):
                value = crystal(source)
                if value:
                    context['multiplier'] *= 1 + state.get(state_key, 0) * value / 100
            value = crystal('hunting_rhythm')
            if value and state.get('crystal_hunt', 0) >= 3:
                state['crystal_hunt'] = 0
                context['multiplier'] *= 1 + value * 3 / 100
                self.log.append(f'{actor.name} 消耗滿層【狩獵節奏】。')
            value = crystal('holy_afterglow')
            stacks = state.pop('crystal_afterglow', 0) if value else 0
            if stacks:
                context['multiplier'] *= 1 + stacks * value / 100
                self.log.append(f'{actor.name} 消耗 {stacks} 層【聖光餘韻】。')
            value = crystal('pure_faith')
            if value and effect == 'holy_light':
                context['multiplier'] *= 1 + state.get('crystal_faith', 0) * value / 100
        if effect in HEALING_EFFECTS:
            bonus = 0
            for source, state_key in (('grace_reserve', 'crystal_grace'),
                                      ('suffering_prayer', 'crystal_suffering'),
                                      ('pure_faith', 'crystal_faith')):
                bonus += state.get(state_key, 0) * crystal(source)
                if source != 'pure_faith' and state.get(state_key, 0):
                    state.pop(state_key, None)
                    self.log.append(f'{actor.name} 消耗源色治療刻印。')
            if bonus:
                context['healing_multiplier'] = context.get('healing_multiplier', 1) * (1 + bonus / 100)
        ready = state.pop('crystal_threefold_ready', False)
        if ready:
            value = crystal('threefold_cast')
            context['multiplier'] *= 1 + value / 100
            context['healing_multiplier'] = context.get('healing_multiplier', 1) * (1 + value / 100)
            context['threefold_cooldown'] = True
            self.log.append(f'{actor.name} 消耗【三重詠唱】完成構圖。')

        # Buff granted by a completed Threefold Hymn affects one damage action.
        if damaging and actor.has('hymn_strike', self.round):
            context['multiplier'] *= 1.1
            context['consume_hymn_strike'] = True

        if self.passive(actor, '裝甲步兵', 1) and skill is not None and damaging:
            last = state.get('chain_last')
            chain = 1 if last == skill.effect else state.get('chain', 0) + 1
            state.update(chain_last=skill.effect, chain=chain)
            if chain >= 3:
                state['chain'] = 0
                context['multiplier'] *= 1.5
                context['chain_break'] = True
                self.log.append(f'{actor.name} 的【百鍊連式】完成，第三式傷害提高 50%！')

        if self.passive(actor, '裝甲步兵', 2) and skill is not None:
            preparation = skill.timing == PREPARATION_TIMING
            if preparation:
                offense = state.pop('offense', 0)
                if offense:
                    amount = self.heal(actor, actor, actor.stats['HP'] * 3 * offense // 100,
                                       passive_trigger=False)
                    self.log.append(f'{actor.name} 消耗 {offense} 層攻勢，恢復 {amount} HP。')
            if damaging:
                guard = state.pop('guard_stance', 0)
                if guard:
                    context['multiplier'] *= 1 + guard * .15
                    self.log.append(f'{actor.name} 消耗 {guard} 層守勢，技能傷害提高 {guard * 15}%。')

        if self.passive(actor, '裝甲步兵', 3) and skill is not None and damaging:
            rage = state.get('blood_rage', 0)
            if rage >= 3:
                state['blood_rage'] = 0
                context['multiplier'] *= 1.3
                context['blood_lifesteal'] = rage == 5
                self.log.append(f'{actor.name} 消耗 {rage} 層血怒，技能傷害提高 30%。')

        if self.passive(actor, '騎士', 1) and skill is not None and damaging:
            revenge = state.pop('revenge', 0)
            if revenge:
                context['multiplier'] *= 1 + revenge * .12
                amount = self.heal(actor, actor, actor.stats['HP'] * revenge * 2 // 100,
                                   passive_trigger=False)
                self.log.append(f'{actor.name} 消耗 {revenge} 層復仇，傷害提高 {revenge * 12}%、恢復 {amount} HP。')

        if self.passive(actor, '騎士', 2) and effect == 'guard' and state.get('watch', 0) >= 2:
            state['watch'] = 0
            context['empowered_guard'] = True
            for ally in self.living(actor.team):
                amount = self.heal(actor, ally, actor.stats['HP'] * 2 // 100,
                                   passive_trigger=False)
                ally.effects['watch_guard'] = self.round + 1
                self.log.append(f'{ally.name} 受到守望誓約保護，恢復 {amount} HP 並獲得 10% 減傷。')

        if self.passive(actor, '騎士', 3):
            combo = state.get('lance_combo')
            if effect == 'knight_charge' and combo == 'opening':
                state.pop('lance_combo', None)
                context['multiplier'] *= 1.25
                self.log.append(f'{actor.name} 消耗破綻，騎士衝鋒傷害提高 25%。')
            elif effect == 'shield_bash' and combo == 'momentum':
                state.pop('lance_combo', None)
                context['shield_followup'] = True

        if self.passive(actor, '弓兵', 3) and damaging and state.get('insight', 0) >= 3:
            state['insight'] = 0
            context.update(multiplier=context['multiplier'] * 1.4,
                           force_hit=True, suppress_insight=True)
            self.log.append(f'{actor.name} 消耗 3 層洞察，本次傷害行動必定命中且傷害提高 40%！')

        if self.passive(actor, '僧侶', 3):
            if effect == 'holy_light':
                discipline = state.pop('discipline', 0)
                radiance = state.pop('radiance', 0)
                context['multiplier'] *= 1 + discipline * .15
                context['healing_multiplier'] = 1 + radiance * .15
                context['cycle_hybrid'] = True
                if discipline or radiance:
                    self.log.append(f'{actor.name} 的聖光消耗 {discipline} 層戒律與 {radiance} 層輝光。')
            elif damaging:
                discipline = state.pop('discipline', 0)
                context['multiplier'] *= 1 + discipline * .15
                context['gain_radiance'] = True
                if discipline:
                    self.log.append(f'{actor.name} 消耗 {discipline} 層戒律，傷害提高 {discipline * 15}%。')
            elif effect in HEALING_EFFECTS:
                radiance = state.pop('radiance', 0)
                context['healing_multiplier'] = 1 + radiance * .15
                context['gain_discipline'] = True
                if radiance:
                    self.log.append(f'{actor.name} 消耗 {radiance} 層輝光，治療提高 {radiance * 15}%。')

        from core import rpg_witch_embroideries
        rpg_witch_embroideries.begin(self, context, DAMAGE_EFFECTS, HEALING_EFFECTS)
        self._passive_action = context
        maze_traits.begin_action(self, context)
        return context

    def _finish_passive_action(self, context):
        from core import rpg_witch_embroideries
        rpg_witch_embroideries.finish(self, context, DAMAGE_EFFECTS, HEALING_EFFECTS)
        actor, skill = context['actor'], context['skill']
        effect = skill.effect if skill is not None else None
        state = actor.passive_state

        def crystal(effect_name):
            return actor.status_stacks.get(f'crystal_{effect_name}', 0)

        if skill is not None and skill.timing == PREPARATION_TIMING and crystal('endless_offense'):
            state['crystal_offense_ready'] = True
        if context['hits']:
            if crystal('endless_arrow'):
                state['crystal_arrows'] = min(10, state.get('crystal_arrows', 0) + context['hits'])
                if state['crystal_arrows'] == 10:
                    self.log.append(f'{actor.name} 的【無盡箭痕】達到 10 層。')
            if crystal('focused_shot') and context.get('target') is not None:
                target_key = id(context['target'])
                if state.get('crystal_focus_target') == target_key:
                    state['crystal_focus'] = min(5, state.get('crystal_focus', 0) + 1)
                else:
                    state.update(crystal_focus_target=target_key, crystal_focus=1)
            if crystal('venom_amplifier') and context.get('target') is not None:
                target = context['target']
                if target.status_stacks.get('poison_arrows'):
                    state['crystal_venom'] = min(5, state.get('crystal_venom', 0) + 1)
            if crystal('hunting_rhythm') and context['critical_hits']:
                state['crystal_hunt'] = min(3, state.get('crystal_hunt', 0)
                                            + context['critical_hits'])
                if state['crystal_hunt'] == 3:
                    self.log.append(f'{actor.name} 的【狩獵節奏】達到 3 層。')
            if crystal('heart_calibration'):
                if context['critical_hits']:
                    state['crystal_calibration'] = 0
                else:
                    state['crystal_calibration'] = min(5, state.get('crystal_calibration', 0)
                                                       + context['hits'])
            if crystal('steel_echo') and actor.has('taunt', self.round):
                state['crystal_steel_echo_stacks'] = min(
                    5, state.get('crystal_steel_echo_stacks', 0) + 1)
        if context['heals']:
            if crystal('grace_reserve'):
                state['crystal_grace'] = min(3, state.get('crystal_grace', 0) + 1)
                if state['crystal_grace'] == 3:
                    self.log.append(f'{actor.name} 的【恩典積累】達到 3 層。')
            if crystal('holy_afterglow'):
                state['crystal_afterglow'] = min(5, state.get('crystal_afterglow', 0) + 1)
        if crystal('pure_faith') and context.get('cleansed'):
            state['crystal_faith'] = min(5, state.get('crystal_faith', 0)
                                         + context['cleansed'])
        if crystal('threefold_cast') and effect in ('heal', 'group_heal', 'bless', 'cleanse'):
            expected = ('heal', 'bless', 'cleanse')
            normalized = 'heal' if effect == 'group_heal' else effect
            sequence = state.get('crystal_threefold_sequence', [])
            sequence = sequence + [normalized] if normalized == expected[len(sequence)] else (
                [normalized] if normalized == 'heal' else [])
            if len(sequence) == 3:
                state['crystal_threefold_ready'] = True
                sequence = []
                self.log.append(f'{actor.name} 完成【三重詠唱】。')
            state['crystal_threefold_sequence'] = sequence
        if context.get('threefold_cooldown') and skill is not None:
            slot = next((rule.slot for rule in actor.rules
                         if rule_skill(actor.job, rule) == skill), None)
            if slot is not None and slot in actor.ready:
                actor.ready[slot] = max(self.round + 1, actor.ready[slot] - 1)

        if context.get('consume_hymn_strike'):
            actor.effects.pop('hymn_strike', None)

        if self.passive(actor, '裝甲步兵', 2) and skill is not None:
            if context['damaging']:
                state['offense'] = min(2, state.get('offense', 0) + 1)
            if skill.timing == PREPARATION_TIMING:
                state['guard_stance'] = min(2, state.get('guard_stance', 0) + 1)
        if self.passive(actor, '裝甲步兵', 3) and effect == 'stance':
            state['blood_rage'] = min(5, state.get('blood_rage', 0) + 1)
        if context.get('blood_lifesteal') and context['actual_damage']:
            amount = self.heal(actor, actor, context['actual_damage'] * 15 // 100,
                               passive_trigger=False)
            self.log.append(f'{actor.name} 的滿層血怒吸血恢復 {amount} HP。')

        if self.passive(actor, '騎士', 3) and context['hits']:
            if effect == 'shield_bash':
                state['lance_combo'] = 'opening'
            elif effect == 'knight_charge':
                state['lance_combo'] = 'momentum'

        if self.passive(actor, '僧侶', 1) and effect in HEALING_EFFECTS and context['heals']:
            grace = state.get('grace', 0)
            if grace >= 3:
                state['grace'] = 0
                base = max(requested for _, requested, amount in context['heals'] if amount)
                healed = [target for target, _, amount in context['heals'] if amount]
                primary = min(healed, key=lambda ally: ally.hp / ally.stats['HP'])
                amount = self.heal(actor, primary, base * 20 // 100, passive_trigger=False)
                total = amount
                others = sorted([ally for ally in self.living(actor.team)
                                 if ally is not primary and ally.hp < ally.stats['HP']],
                                key=lambda ally: ally.hp / ally.stats['HP'])[:3]
                for ally in others:
                    total += self.heal(actor, ally, base * 15 // 100, passive_trigger=False)
                self.log.append(f'{actor.name} 的【恩典回響】額外恢復全隊共 {total} HP。')
            else:
                state['grace'] = grace + 1

        if self.passive(actor, '僧侶', 2) and state.get('hymn_procs', 0) < 3:
            verses = set(state.get('hymn_verses', []))
            if effect in HYMN_HEALING_EFFECTS and context['heals']:
                verses.add('mercy')
            if effect == 'bless' and context.get('hymn_bless'):
                verses.add('courage')
            if effect == 'cleanse' and context.get('cleansed', 0):
                verses.add('purity')
            state['hymn_verses'] = sorted(verses)
            if len(verses) == 3:
                state['hymn_verses'] = []
                state['hymn_procs'] = state.get('hymn_procs', 0) + 1
                total = 0
                for ally in self.living(actor.team):
                    total += self.heal(actor, ally, actor.stats['治療量'] // 10,
                                       passive_trigger=False)
                    ally.effects['hymn_strike'] = self.round + 1
                self.log.append(f'{actor.name} 完成【三重聖歌】：全隊恢復 {total} HP，下一次傷害行動提高 10%。')

        if self.passive(actor, '僧侶', 3) and not context.get('cycle_hybrid'):
            if context.get('gain_radiance'):
                state['radiance'] = min(3, state.get('radiance', 0) + 1)
            if context.get('gain_discipline') and context['heals']:
                state['discipline'] = min(3, state.get('discipline', 0) + 1)

        maze_traits.end_action(self, context)
        self._passive_action = None

    def basic_attack(self, actor, target):
        context = self._begin_passive_action(actor, target=target, basic=True)
        try:
            return self.hit(actor, target)
        finally:
            self._finish_passive_action(context)

    def heal(self, actor, target, requested, trigger_share=True, passive_trigger=True, maze_trigger=True):
        context = self._passive_action if self._passive_action and self._passive_action['actor'] is actor else None
        if (context and context['skill'] and context['skill'].effect in HEALING_EFFECTS
                and passive_trigger and actor.status_stacks.get('embroidery_witch_wings')
                and target.hp * 2 < target.stats['HP']):
            requested = int(requested * 1.05)
        if context is not None:
            requested = int(requested * context.get('healing_multiplier', 1))
        passive_base = requested
        requested = max(0, int(requested * self.healing_done_multiplier(actor)
                               * self.healing_received_multiplier(target)))
        amount = min(target.stats['HP'] - target.hp, requested)
        target.hp += amount
        actor.combat_stats['healing_done'] += amount
        actor.combat_stats['overhealing'] += requested - amount
        target.combat_stats['healing_received'] += amount
        maze_traits.healed(self, target, amount, maze_trigger and context is not None and context['skill'] is not None)
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
        if amount and passive_trigger and context is not None:
            context['heals'].append((target, passive_base, amount))
        if amount and actor.status_stacks.get('crystal_life_lance') and target is actor:
            actor.passive_state['crystal_life_lance_stacks'] = min(
                5, actor.passive_state.get('crystal_life_lance_stacks', 0) + 1)
        return amount

    @staticmethod
    def restore(target, requested):
        """Restore HP without attributing skill healing to another fighter."""
        amount = min(target.stats['HP'] - target.hp, max(0, int(requested)))
        target.hp += amount
        target.combat_stats['healing_received'] += amount
        return amount

    def _gain_watch(self, protected):
        for knight in self.living(protected.team):
            source_watch = knight.status_stacks.get('crystal_guardian_oath', 0)
            if (knight is protected or not (self.passive(knight, '騎士', 2) or source_watch)
                    or not protected.has('guard', self.round)):
                continue
            source_id = protected.effect_sources.get('guard')
            if source_id is not None and source_id != knight.user_id:
                continue
            state = knight.passive_state
            seen = state.setdefault('watch_seen', {})
            key = str(protected.user_id if protected.user_id is not None else id(protected))
            if seen.get(key) == self.round:
                continue
            seen[key] = self.round
            before = state.get('watch', 0)
            state['watch'] = min(2, before + 1)
            if state['watch'] != before:
                self.log.append(f'{knight.name} 的守望增加至 {state["watch"]}/2 層。')
            if source_watch:
                state['crystal_guardian_oath_stacks'] = min(
                    3, state.get('crystal_guardian_oath_stacks', 0) + 1)

    def _direct_damage_taken(self, target, actual):
        if not actual or target.team != 0:
            return
        state = target.passive_state
        for source, key in (('blooded_blade', 'crystal_blooded'),
                            ('vengeance_mark', 'crystal_vengeance')):
            if target.status_stacks.get(f'crystal_{source}'):
                round_key = f'{key}_round'
                if state.get(round_key) != self.round:
                    state[round_key] = self.round
                    state[key] = min(5, state.get(key, 0) + 1)
        if target.status_stacks.get('crystal_immovable_wall'):
            state['crystal_wall'] = max(0, state.get('crystal_wall', 0) - 2)
        for monk in self.living(target.team):
            if (monk.status_stacks.get('crystal_suffering_prayer')
                    and target.hp * 100 <= target.stats['HP'] * 40):
                seen = monk.passive_state.setdefault('crystal_suffering_seen', [])
                key = target.user_id if target.user_id is not None else id(target)
                if key not in seen:
                    seen.append(key)
                    monk.passive_state['crystal_suffering'] = min(
                        3, monk.passive_state.get('crystal_suffering', 0) + 1)
        if self.passive(target, '裝甲步兵', 3) and state.get('blood_round') != self.round:
            state['blood_round'] = self.round
            state['blood_rage'] = min(5, state.get('blood_rage', 0) + 2)
            self.log.append(f'{target.name} 的血怒增加至 {state["blood_rage"]}/5 層。')
        if (self.passive(target, '騎士', 1) and target.has('taunt', self.round)
                and state.get('revenge_round') != self.round):
            state['revenge_round'] = self.round
            state['revenge'] = min(3, state.get('revenge', 0) + 1)
            self.log.append(f'{target.name} 的復仇增加至 {state["revenge"]}/3 層。')
        self._gain_watch(target)

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

    def apply_damage(self, target, damage, share_link=True, direct=False):
        """Apply final damage, including tarot survival and Lovers sharing."""
        partner = None
        if share_link and target.team == 0 and target.linked_user_id is not None:
            partner = next((fighter for fighter in self.living(0)
                            if fighter.user_id == target.linked_user_id and fighter is not target), None)

        def apply_one(victim, requested):
            before = victim.hp
            requested = maze_traits.before_damage(self, victim, requested, direct)
            crystal_shield = victim.status_stacks.get('crystal_shield', 0)
            if crystal_shield > 0:
                absorbed = min(crystal_shield, max(0, int(requested)))
                victim.status_stacks['crystal_shield'] -= absorbed
                requested -= absorbed
                if absorbed:
                    self.log.append(f'{victim.name} 的底色護幕吸收 {absorbed} 傷害。')
            if (victim.team == 1 and victim.is_boss
                    and self.mechanics.get('maze_final')
                    and self.mechanics.get('maze_final_shield', 0) > 0):
                shield = self.mechanics['maze_final_shield']
                absorbed = min(shield, max(0, int(requested)))
                self.mechanics['maze_final_shield'] -= absorbed
                requested -= absorbed
                if absorbed:
                    self.log.append(
                        f'{victim.name} 的畫幕護盾吸收 {absorbed} 傷害，剩餘 '
                        f'{self.mechanics["maze_final_shield"]}。')
            actual = min(before, max(0, int(requested)))
            if (actual >= before and before > 1
                    and victim.status_stacks.get('crystal_survive_fatal_once')
                    and not victim.status_stacks.get('crystal_survive_fatal_used')):
                actual = before - 1
                victim.status_stacks['crystal_survive_fatal_used'] = 1
                self.log.append(f'{victim.name} 的【留白保命】保留 1 HP。')
            if (actual >= before and before > 0 and victim.fortune_card == 'judgement'
                    and not victim.effects.get('fortune_judgement_used')):
                actual = max(0, before - 1)
                victim.effects['fortune_judgement_used'] = True
                self.log.append(f'{victim.name} 的【審判】生效，在致死傷害中保留 1 HP。')
            actual = maze_traits.survive(self, victim, actual, direct)
            from core import rpg_witch_embroideries
            actual = rpg_witch_embroideries.survive(self, victim, actual, direct)
            victim.hp -= actual
            maze_traits.after_damage(self, victim)
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

    def clear_negative_effects(self, target):
        """Remove every player-cleansable debuff, including stacked poison arrows."""
        removed = 0
        for effect in ('poison', 'break', 'stun', 'weak'):
            removed += target.effects.pop(effect, None) is not None
            target.effect_sources.pop(effect, None)
        removed += target.status_stacks.pop('corruption', None) is not None
        removed += target.status_stacks.pop('drowning_mark', None) is not None
        removed += target.status_stacks.pop('poison_arrows', None) is not None
        return removed

    def apply_debuff(self, target, effect, until, source=None):
        """Apply a cleansable debuff unless Guard currently grants immunity."""
        if target.job == '逆潮法陣' and effect in ('poison', 'stun'):
            self.log.append(f'{target.name} 免疫{"中毒" if effect == "poison" else "暈眩"}。')
            return False
        if (effect == 'break' and target.is_boss and self.mechanics.get('maze_final')
                and self.mechanics.get('maze_final_shield', 0) > 0):
            self.log.append(f'{target.name} 的畫幕護盾阻止了破甲。')
            return False
        if target.has('immunity', self.round):
            self.log.append(f'{target.name} 受到【護衛】保護，免疫負面狀態。')
            self._gain_watch(target)
            return False
        if source is not None and source.team == 0 and target.team != source.team and effect in ('poison', 'weak'):
            until += source.status_stacks.get('maze_debuff_duration_bonus', 0)
        target.effects[effect] = max(target.effects.get(effect, 0), until)
        if source is not None and source.user_id is not None:
            target.effect_sources[effect] = source.user_id
        return True

    def tick_poison_arrows(self, target):
        """Resolve every independently tracked poison-arrow payload once per turn."""
        stacks = target.status_stacks.get('poison_arrows', [])
        remaining = []
        for stack in stacks:
            if self.round < stack['next_round']:
                remaining.append(stack)
                continue
            source_id = stack.get('source_id')
            source = (None if source_id is None else
                      next((f for f in self.fighters if f.user_id == source_id), None))
            damage = max(1, int(stack['damage']))
            actual, partner, partner_actual = self.apply_damage(target, damage)
            total = actual + partner_actual
            if source is not None and source is not target:
                source.combat_stats['damage_dealt'] += total
                source.combat_stats['support_damage'] += total
                source.combat_stats['knockouts'] += int(actual and target.hp == 0)
                if partner is not None:
                    source.combat_stats['knockouts'] += int(partner_actual and partner.hp == 0)
                if self.passive(source, '弓兵', 2) and total:
                    toxicity = target.status_stacks.setdefault('passive_toxicity', {})
                    key = str(source.user_id if source.user_id is not None else id(source))
                    toxicity[key] = min(3, toxicity.get(key, 0) + 1)
                    self.log.append(f'{target.name} 的毒性增加至 {toxicity[key]}/3 層。')
            stack['remaining'] -= 1
            stack['next_round'] += 1
            self.log.append(f'{target.name} 受到毒箭侵蝕，損失 {total} HP。')
            if stack['remaining'] > 0:
                remaining.append(stack)
            if target.hp <= 0:
                remaining.clear()
                break
        if remaining:
            target.status_stacks['poison_arrows'] = remaining
        else:
            target.status_stacks.pop('poison_arrows', None)
        return target.hp > 0

    def check_end(self):
        for defeated in [f for f in self.fighters if f.team == 1 and f.hp <= 0
                         and not f.status_stacks.get('mechanic_defeat_processed')]:
            if defeated.job == '蝕光星核':
                defeated.status_stacks['mechanic_defeat_processed'] = 1
                giant = next((f for f in self.living(1) if f.job == '星蝕巨神'), None)
                self.mechanics['star_core_down_round'] = self.round
                self.mechanics['star_charging'] = False
                if giant is not None:
                    giant.effects['break'] = self.round + 1
                    self.log.append(f'{defeated.name} 崩解，{giant.name} 的星蝕墜落取消並破甲至第 {self.round + 1} 回合結束。')
            elif defeated.job == '逆潮法陣':
                defeated.status_stacks['mechanic_defeat_processed'] = 1
                saint = next((f for f in self.living(1) if f.job == '逆潮聖骸'), None)
                if saint is not None:
                    saint.effects['break'] = self.round + 1
                    self.log.append(f'{defeated.name} 被擊破，{saint.name} 破甲至第 {self.round + 1} 回合結束。')
        master = next((f for f in self.fighters if f.team == 1 and f.job == '王城傀儡師'), None)
        if master is not None and master.hp <= 0:
            for puppet in (f for f in self.fighters if f.team == 1 and f.job in ('劍傀儡', '咒傀儡')):
                puppet.hp = 0
        twins = [f for f in self.fighters if f.team == 1 and f.job in ('赤雷', '蒼炎')]
        living_twins = [f for f in twins if f.hp > 0]
        if (len(twins) == 2 and len(living_twins) == 1
                and not self.mechanics.get('twin_revive_job')):
            survivor = living_twins[0]
            fallen = next(f for f in twins if f.hp <= 0)
            base_attacks = self.mechanics.get('twin_base_attacks', {})
            base_speeds = self.mechanics.get('twin_base_speeds', {})
            survivor.stats['攻擊'] = max(1, base_attacks.get(survivor.job, survivor.stats['攻擊']) * 125 // 100)
            survivor.speed = base_speeds.get(survivor.job, survivor.speed) + 15
            self.mechanics.update(twin_revive_job=fallen.job,
                                  twin_revive_round=self.round + 2,
                                  twin_revive_delayed=False)
            self.log.append(f'{survivor.name} 進入【孤獸暴走】，兩回合後將以【再生共鳴】復活 {fallen.name}！')
        if not self.living(0) and not self.living(1):
            self.result = '平手'
        elif not self.living(1):
            self.result = '勝利'
        elif not self.living(0):
            self.result = '戰敗'
        return self.result is not None

    def target(self, actor, candidates, rule, offensive=False):
        if rule.target == 'debuffed':
            candidates = [f for f in candidates if any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun', 'weak'))
                          or f.status_stacks.get('corruption', 0) or f.status_stacks.get('drowning_mark', 0)
                          or f.status_stacks.get('poison_arrows')]
        if offensive:
            taunters = [f for f in candidates if f.has('taunt', self.round)]
            candidates = taunters or candidates
        if rule.target == 'self':
            return actor if actor in candidates else None
        if not candidates:
            return None
        if rule.target == 'debuffed':
            return max(candidates, key=lambda f: (self.cleansable_stack_count(f),
                                                   -f.hp / f.stats['HP']))
        if rule.target == 'boss':
            preferred = [f for f in candidates if f.is_boss]
            candidates = preferred or candidates
        elif rule.target == 'add':
            preferred = [f for f in candidates if not f.is_boss]
            candidates = preferred or candidates
        elif rule.target == 'mechanic':
            priority = max((self.mechanic_priority(f) for f in candidates), default=0)
            if priority:
                candidates = [f for f in candidates if self.mechanic_priority(f) == priority]
        if rule.target == 'strongest':
            return max(candidates, key=lambda f: f.stats['攻擊'])
        if rule.target == 'highest_hp':
            return max(candidates, key=lambda f: f.hp / f.stats['HP'])
        if offensive and actor.team == 1:
            return self.rng.choice(candidates)
        return min(candidates, key=lambda f: f.hp / f.stats['HP'])

    def is_charging(self, fighter):
        return (fighter.has('charging', self.round) or fighter.has('charged_punch', self.round) or
                (fighter.job == '深淵鐘龍' and self.mechanics.get('clock_charging')) or
                (fighter.job == '城崎諾亞' and (self.mechanics.get('noah_draft_charging')
                                                        or self.mechanics.get('noah_final_charging'))) or
                (fighter.job in ('赤雷', '蒼炎') and self.mechanics.get('twin_revive_job')) or
                (fighter.job == '吞城鯨' and self.mechanics.get('whale_swallow_charging')) or
                (fighter.job == '熔爐鎧獸' and self.mechanics.get('furnace_charging')) or
                (fighter.job == '星蝕巨神' and self.mechanics.get('star_charging')))

    def has_breakable_guard(self, fighter):
        return (fighter.has('breakable_guard', self.round) or
                (fighter.job == '深淵鐘龍' and self.mechanics.get('clock_armor', 0) > 0) or
                (fighter.job == '吞城鯨' and self.mechanics.get('whale_shield', 0) > 0) or
                (fighter.job == '熔爐鎧獸' and self.mechanics.get('furnace_armor', 0) > 0))

    def mechanic_priority(self, fighter):
        return max(fighter.mechanic_priority,
                   1 if fighter.has('mechanic_target', self.round) else 0)

    @staticmethod
    def cleansable_stack_count(fighter):
        arrows = fighter.status_stacks.get('poison_arrows', ())
        return (max(0, int(fighter.status_stacks.get('corruption', 0)))
                + max(0, int(fighter.status_stacks.get('drowning_mark', 0))) + len(arrows))

    def select(self, actor):
        allies, enemies = self.living(actor.team), self.living(1 - actor.team)
        for rule in sorted(actor.rules, key=lambda r: r.priority):
            skill = rule_skill(actor.job, rule)
            if skill.effect == 'knight_charge' and rule.target == 'self':
                # Existing knight slot-3 tactics targeted self when this skill was 堅守.
                condition = 'always' if rule.condition == 'self40' else rule.condition
                rule = Rule(rule.slot, rule.priority, rule.enabled, condition, 'lowest',
                            rule.skill_id, condition_value(condition, rule.condition_value))
            if not rule.enabled or self.round < actor.ready.get(rule.slot, 0):
                continue
            taunters = [f for f in enemies if f.has('taunt', self.round)]
            targetable_enemies = (enemies if skill.effect in ('area', 'cleave', 'holy_light')
                                  else taunters or enemies)
            threshold = condition_value(rule.condition, rule.condition_value)
            if rule.condition == 'self40' and actor.hp * 100 > actor.stats['HP'] * threshold:
                continue
            if rule.condition == 'ally50' and not any(f.hp * 100 <= f.stats['HP'] * threshold for f in allies):
                continue
            if rule.condition == 'allies_injured' and sum(f.hp < f.stats['HP'] for f in allies) < threshold:
                continue
            if rule.condition == 'enemies3' and len(enemies) < threshold:
                continue
            condition_enemies = targetable_enemies
            if rule.condition == 'enemy_hp_lte':
                condition_enemies = [f for f in targetable_enemies
                                     if f.hp * 100 <= f.stats['HP'] * threshold]
            elif rule.condition == 'enemy_hp_gte':
                condition_enemies = [f for f in targetable_enemies
                                     if f.hp * 100 >= f.stats['HP'] * threshold]
            elif rule.condition == 'enemy_charging':
                condition_enemies = [f for f in targetable_enemies if self.is_charging(f)]
            elif rule.condition == 'enemy_guard':
                condition_enemies = [f for f in targetable_enemies if self.has_breakable_guard(f)]
            elif rule.condition == 'enemy_broken':
                condition_enemies = [f for f in targetable_enemies if f.has('break', self.round)]
            elif rule.condition == 'enemy_add':
                condition_enemies = [f for f in targetable_enemies if not f.is_boss]
            elif rule.condition == 'mechanic_target':
                condition_enemies = [f for f in targetable_enemies if self.mechanic_priority(f)]
            if rule.condition in ('enemy_hp_lte', 'enemy_hp_gte', 'enemy_charging',
                                  'enemy_guard', 'enemy_broken', 'enemy_add',
                                  'mechanic_target') and not condition_enemies:
                continue
            if rule.condition == 'round_gte' and self.round < threshold:
                continue
            if rule.condition == 'ally_debuff' and not any(
                    any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun', 'weak'))
                    or f.status_stacks.get('corruption', 0) or f.status_stacks.get('drowning_mark', 0)
                    or f.status_stacks.get('poison_arrows') for f in allies):
                continue
            if rule.condition == 'ally_debuff_stacks' and not any(
                    self.cleansable_stack_count(f) >= threshold for f in allies):
                continue
            candidates = allies if skill.effect in ALLY_EFFECTS else enemies
            if skill.effect not in ALLY_EFFECTS:
                candidates = condition_enemies
            elif rule.condition == 'ally50' and skill.effect in ('heal', 'holy_light'):
                candidates = [f for f in candidates if f.hp * 100 <= f.stats['HP'] * threshold]
            elif rule.condition == 'ally_debuff_stacks':
                candidates = [f for f in candidates if self.cleansable_stack_count(f) >= threshold]
            if skill.effect in ('heal', 'group_heal'):
                candidates = [f for f in candidates if f.hp < f.stats['HP']]
            elif skill.effect == 'cleanse':
                candidates = [f for f in candidates if any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun', 'weak'))
                              or f.status_stacks.get('corruption', 0) or f.status_stacks.get('drowning_mark', 0)
                              or f.status_stacks.get('poison_arrows')]
            elif skill.effect == 'bless':
                candidates = [f for f in candidates if not f.has(skill.effect, self.round)]
            if skill.effect == 'group_heal':
                target = actor if candidates else None
            elif skill.effect == 'rally':
                target = actor if actor.hp < actor.stats['HP'] else None
            elif skill.effect == 'guard':
                bonus = max(1, actor.stats['防禦'])
                target = actor if any(not f.has('guard', self.round) or f.guard_bonus < bonus for f in allies) else None
            elif skill.effect in ('stance', 'taunt'):
                target = actor if not actor.has(skill.effect, self.round) else None
            else:
                target = self.target(actor, candidates, rule, skill.effect not in ALLY_EFFECTS)
            if target:
                return rule, skill, target
        return None

    def hit(self, actor, target, power=1.0, precise=False, lifesteal=None, attack_override=None,
            counterable=True, attack_scope='single', passive_trigger=True):
        if actor.team == 0 and not actor.armed:
            self.log.append(f'{actor.name} 未裝備武器，無法造成傷害。')
            return False
        actor.combat_stats['attacks'] += 1
        context = (self._passive_action if passive_trigger and self._passive_action
                   and self._passive_action['actor'] is actor else None)
        passive_multiplier = context.get('multiplier', 1) if context else 1
        force_hit = bool(context and context.get('force_hit'))
        tempo = None
        if passive_trigger and self.passive(actor, '弓兵', 1):
            tempo = actor.passive_state.get('arrow_tempo', 0)
            passive_multiplier *= 1 + tempo * .03
        stored_attack = actor.stored_defense_attack
        if stored_attack:
            actor.stored_defense_attack = 0
            self.log.append(f'{actor.name} 釋放【吞城鯨飾品】蓄積的 {stored_attack} 點攻擊力。')
        chance = self.hit_chance(actor, target)
        if not precise and not force_hit and self.rng.random() * 100 >= chance:
            actor.combat_stats['misses'] += 1
            if actor.status_stacks.get('crystal_endless_arrow'):
                actor.passive_state['crystal_arrows'] = max(
                    0, actor.passive_state.get('crystal_arrows', 0) - 2)
            if tempo is not None:
                actor.passive_state['arrow_tempo'] = 0
                self.log.append(f'{actor.name} 的箭勢因未命中而歸零。')
            self.log.append(f'{actor.name} → {target.name}：未命中')
            return False
        base_attack = actor.stats['攻擊'] if attack_override is None else attack_override
        base_attack += stored_attack
        if (actor.alternating_damage_percent and
                (self.round % 2 == 1 and attack_scope == 'single' or
                 self.round % 2 == 0 and attack_scope == 'group')):
            base_attack *= (100 + actor.alternating_damage_percent) / 100
        base_attack *= 1.2 if actor.job == '裝甲步兵' and actor.has('stance', self.round) else 1
        base_attack *= 0.8 if actor.has('weak', self.round) else 1
        blessed = actor.has('bless', self.round)
        paint_attack = (base_attack * self.damage_dealt_multiplier(actor)
                        * self.direct_damage_multiplier(actor)
                        * self.debuff_damage_multiplier(actor, target)
                        * self.crystal_damage_multiplier(actor, target))
        attack = paint_attack * (1.25 if blessed else 1) * maze_traits.damage_multiplier(actor, self.round)
        base_defense = target.stats['防禦']
        if target.has('guard', self.round):
            base_defense += target.guard_bonus
        broken = target.has('break', self.round)
        defense = 0 if broken else base_defense
        low, high = actor.stability
        stability = self.rng.randint(low, high) if low != high else low
        critical = self.rng.random() * 100 < self.critical_chance(actor)
        guarded = bool(target.damage_guard_chance and self.rng.random() * 100 < target.damage_guard_chance)
        puppet_shield = (target.job == '王城傀儡師'
                         and any(f.job == '咒傀儡' for f in self.living(target.team))
                         and not self.mechanics.get('puppet_phase_two'))
        defense_effectiveness = DEFENSE_EFFECTIVENESS.get(target.job, 0.35)

        def final_damage(attack_value, defense_value):
            value = max(1, int(attack_value * power - defense_value * defense_effectiveness))
            value = max(1, value * stability // 100)
            if critical:
                calibration = (actor.passive_state.get('crystal_calibration', 0)
                               * actor.status_stacks.get('crystal_heart_calibration', 0))
                value = max(1, value * (actor.critical_damage_percent + calibration) // 100)
            if target.has('stance', self.round):
                multiplier = {'民兵': 0.8}.get(target.job, 0.65)
                value = max(1, int(value * multiplier))
            if target.has('taunt', self.round) and target.job != '騎士':
                value = max(1, int(value * 0.85))
            if target.has('noah_blue_guard', self.round):
                value = max(1, value * self.mechanics.get('noah_blue_reduction', 0) // 100)
            if puppet_shield:
                value = max(1, value // 2)
            if target.job == '吞城鯨' and self.mechanics.get('whale_shield', 0) > 0:
                value = max(1, value * 65 // 100)
            if target.job == '熔爐鎧獸' and self.mechanics.get('furnace_armor', 0) > 0:
                value = max(1, value * (100 - self.mechanics['furnace_armor'] * 8) // 100)
            if (target.job == '星蝕巨神'
                    and any(f.job == '蝕光星核' for f in self.living(target.team))):
                value = max(1, value * 80 // 100)
            if guarded:
                value = max(1, value // 2)
            value = max(1, int(value * self.damage_taken_multiplier(target)))
            if target.has('watch_guard', self.round):
                value = max(1, value * 90 // 100)
            value = max(1, int(value * passive_multiplier))
            return value

        damage = final_damage(attack, defense)
        undefended_damage = final_damage(attack, 0)
        base_damage = final_damage(paint_attack, base_defense)
        broken_damage = final_damage(paint_attack, defense)
        vulnerable = target.has('vulnerable', self.round)
        pre_vulnerable_damage = damage
        if vulnerable:
            damage = max(1, damage * 110 // 100)
            undefended_damage = max(1, undefended_damage * 110 // 100)
        actual_damage = min(target.hp, damage)
        pre_vulnerable_actual = min(target.hp, pre_vulnerable_damage)
        base_actual = min(target.hp, base_damage)
        broken_actual = min(target.hp, broken_damage)
        break_assist = max(0, broken_actual - base_actual) if broken else 0
        bless_assist = max(0, pre_vulnerable_actual - broken_actual) if blessed else 0
        vulnerable_assist = max(0, actual_damage - pre_vulnerable_actual) if vulnerable else 0
        target_hp_before = target.hp
        target_actual, partner, partner_actual = self.apply_damage(target, damage, direct=True)
        actual_damage = target_actual + partner_actual
        actor.combat_stats['hits'] += 1
        actor.combat_stats['critical_hits'] += int(critical)
        if context is not None:
            context['critical_hits'] += int(critical)
        actor.combat_stats['damage_dealt'] += actual_damage
        self._maze_final_hit(actor, target, target_hp_before, actual_damage)
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
        if target_actual and target.hp == 0:
            kill_heal = actor.status_stacks.get('crystal_kill_heal_percent', 0)
            if kill_heal:
                restored = self.restore(actor, actor.stats['HP'] * kill_heal // 100)
                if restored:
                    self.log.append(f'{actor.name} 的【終筆回生】恢復 {restored} HP。')
        if partner is not None:
            actor.combat_stats['knockouts'] += int(partner_actual and partner.hp == 0)
        self.log.append(f'{actor.name} → {target.name}：{damage} 傷害{"（暴擊）" if critical else ""}'
                        f'{f"（穩定度 {stability}%）" if actor.stability != (100, 100) else ""}'
                        f'{"（套裝減傷）" if guarded else ""}{"，倒下" if target.hp == 0 else ""}')
        if context is not None:
            context['hits'] += 1
            context['actual_damage'] += actual_damage
            if (context.get('chain_break') and target.hp > 0
                    and id(target) not in context['chain_broken']):
                context['chain_broken'].add(id(target))
                if self.apply_debuff(target, 'break', self.round + 1, actor):
                    self.log.append(f'{target.name} 被百鍊連式破甲至第 {self.round + 1} 回合結束。')
            if (self.passive(actor, '弓兵', 3) and critical
                    and not context.get('suppress_insight')):
                state = actor.passive_state
                state['insight'] = min(3, state.get('insight', 0) + 1)
        self._direct_damage_taken(target, target_actual)
        if partner is not None:
            self._direct_damage_taken(partner, partner_actual)
        if target.team == 0 and target.defense_conversion:
            target.stored_defense_attack = max(0, undefended_damage - damage)
            if target.stored_defense_attack:
                self.log.append(f'{target.name} 的【吞城鯨飾品】蓄積 {target.stored_defense_attack} 點攻擊力。')
        if actor.team == 0 and target.job == '深淵鐘龍' and self.mechanics.get('clock_charging'):
            layers = self.mechanics.get('clock_armor', 0)
            if layers > 0:
                removed = 2 if precise or critical else 1
                self.mechanics['clock_armor'] = max(0, layers - removed)
                self.log.append(f'{target.name} 的鐘甲減少 {min(layers, removed)} 層，剩餘 {self.mechanics["clock_armor"]} 層。')
        if (actor.team == 0 and target.job == '吞城鯨'
                and self.mechanics.get('whale_shield', 0) > 0
                and attack_scope == 'single'
                and (power >= 1.5 or critical or
                     attack_override is not None and attack_override >= actor.stats['攻擊'] * 1.5)):
            self.mechanics['whale_shield'] -= 1
            layers = self.mechanics['whale_shield']
            self.log.append(f'{target.name} 的城塞鯨脂減少 1 層，剩餘 {layers} 層。')
            if layers == 0 and target.hp > 0:
                target.effects['break'] = self.round + 2
                self.log.append(f'{target.name} 的【城塞鯨脂】完全崩解，遭到破甲至第 {self.round + 2} 回合結束。')
        if (actor.team == 0 and target.job == '熔爐鎧獸'
                and self.mechanics.get('furnace_armor', 0) > 0
                and attack_scope == 'single' and (power >= 1.5 or critical)):
            self.mechanics['furnace_armor'] -= 1
            layers = self.mechanics['furnace_armor']
            self.log.append(f'{target.name} 的爐甲減少 1 層，剩餘 {layers} 層。')
            if layers == 0 and target.hp > 0:
                target.effects['break'] = self.round + 1
                self.mechanics['furnace_charging'] = False
                self.log.append(f'{target.name} 的爐甲完全崩解，爐心震爆取消並破甲至第 {self.round + 1} 回合結束。')
        if actor.team == 0 and target.hp == 0 and target.job == '蝕光星核':
            target.status_stacks['mechanic_defeat_processed'] = 1
            giant = next((f for f in self.living(1) if f.job == '星蝕巨神'), None)
            self.mechanics['star_core_down_round'] = self.round
            self.mechanics['star_charging'] = False
            if giant is not None:
                giant.effects['break'] = self.round + 1
                self.log.append(f'{target.name} 崩解，{giant.name} 的星蝕墜落取消並破甲至第 {self.round + 1} 回合結束。')
        if actor.team == 0 and target.hp == 0 and target.job == '逆潮法陣':
            target.status_stacks['mechanic_defeat_processed'] = 1
            saint = next((f for f in self.living(1) if f.job == '逆潮聖骸'), None)
            if saint is not None:
                saint.effects['break'] = self.round + 1
                self.log.append(f'{target.name} 被擊破，{saint.name} 破甲至第 {self.round + 1} 回合結束。')
        if (actor.team == 0 and target.hp > 0 and actor.vulnerable_chance
                and self.rng.random() * 100 < actor.vulnerable_chance):
            target.effects['vulnerable'] = self.round + 1
            if actor.user_id is not None:
                target.effect_sources['vulnerable'] = actor.user_id
            else:
                target.effect_sources.pop('vulnerable', None)
            self.log.append(f'{target.name} 陷入易傷，受到的直接傷害 +{actor.vulnerable_percent}% 至第 {self.round + 1} 回合結束。')
        drain = actor.lifesteal if lifesteal is None else lifesteal
        healing = self.heal(actor, actor, actual_damage * drain // 100, maze_trigger=False)
        if healing > 0 and actor.hp > 0:
            self.log.append(f'{actor.name} 吸血恢復 {healing} HP')
        if (counterable and target.job == '騎士' and target.hp > 0 and actor.hp > 0
                and target.has('taunt', self.round)
                and target.team != actor.team):
            self.log.append(f'{target.name} 發動【挑釁反擊】！')
            self.hit(target, actor, 1.0, counterable=False)
        if passive_trigger and tempo is not None:
            if tempo >= 6:
                actor.passive_state['arrow_tempo'] = 0
                self.log.append(f'{actor.name} 的【無間箭勢】滿層，追加一支箭！')
                if actor.hp > 0 and target.hp > 0:
                    self.hit(actor, target, .8, counterable=False, passive_trigger=False)
            else:
                actor.passive_state['arrow_tempo'] = tempo + 1
        if (passive_trigger and self.passive(actor, '弓兵', 2) and target.hp > 0):
            toxicity = target.status_stacks.get('passive_toxicity', {})
            key = str(actor.user_id if actor.user_id is not None else id(actor))
            if toxicity.get(key, 0) >= 3:
                toxicity[key] = 0
                damage = max(1, actor.stats['攻擊'] * 180 // 100)
                dealt, linked, linked_damage = self.apply_damage(target, damage)
                total = dealt + linked_damage
                actor.combat_stats['damage_dealt'] += total
                actor.combat_stats['direct_damage'] += total
                actor.combat_stats['knockouts'] += int(dealt and target.hp == 0)
                if linked is not None:
                    actor.combat_stats['knockouts'] += int(linked_damage and linked.hp == 0)
                self.log.append(f'{actor.name} 引爆猛毒，{target.name} 受到 {total} 點無視防禦傷害。')
        maze_traits.after_hit(self, actor, target, actual_damage, context)
        return True

    def add_corruption(self, target, amount=1):
        if target.has('immunity', self.round):
            self.log.append(f'{target.name} 受到【護衛】保護，免疫腐敗。')
            return
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

    def twin_act(self, actor):
        """Alternate the twins' turns while preserving their shared revive race."""
        if actor.job == '赤雷':
            if self.round % 2 == 0:
                return
            self.record_skill(actor, '雷牙突襲')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            self.log.append(f'{actor.name} 使用【雷牙突襲】')
            if target is not None:
                self.hit(actor, target)
            return
        if self.round % 2 == 1:
            return
        skill = '雷炎吐息' if self.round % 4 == 0 else '蒼炎吐息'
        power = 1.2 if self.round % 4 == 0 else 0.65
        self.record_skill(actor, skill)
        self.log.append(f'{actor.name} 使用【{skill}】：對全體造成 {power * 100:g}% 傷害！')
        for enemy in self.living(0):
            if actor.hp > 0 and enemy.hp > 0:
                self.hit(actor, enemy, power, attack_scope='group')

    def process_twin_revival(self):
        revive_job = self.mechanics.get('twin_revive_job')
        if not revive_job or self.round < self.mechanics.get('twin_revive_round', 10**9):
            return
        survivor = next((f for f in self.living(1) if f.job in ('赤雷', '蒼炎')), None)
        fallen = next((f for f in self.fighters if f.team == 1 and f.job == revive_job), None)
        if survivor is None or fallen is None:
            return
        fallen.hp = max(1, fallen.stats['HP'] * 20 // 100)
        base_attacks = self.mechanics.get('twin_base_attacks', {})
        base_speeds = self.mechanics.get('twin_base_speeds', {})
        survivor.stats['攻擊'] = base_attacks.get(survivor.job, survivor.stats['攻擊'])
        survivor.speed = base_speeds.get(survivor.job, survivor.speed)
        self.mechanics.pop('twin_revive_job', None)
        self.mechanics.pop('twin_revive_round', None)
        self.mechanics.pop('twin_revive_delayed', None)
        self.log.append(f'{fallen.name} 的【再生共鳴】完成，以 {fallen.hp} HP 復活；孤獸暴走解除。')

    def whale_act(self, actor):
        old_tide = self.mechanics.get('whale_tide', 0)
        tide = min(100, old_tide + 10)
        self.mechanics['whale_tide'] = tide
        if old_tide < 30 <= tide:
            self.mechanics['whale_flooded_streets'] = True
            self.log.append(f'{actor.name} 的水位達到 30：【淹沒街道】使全體攻擊傷害提高 15%。')
        if old_tide < 50 <= tide:
            for enemy in self.living(0):
                enemy.speed = max(1, enemy.speed - 10)
            self.log.append(f'{actor.name} 的水位達到 50：【深海壓迫】使全隊速度永久降低 10。')
        if old_tide < 70 <= tide:
            for enemy in self.living(0):
                enemy.healing_received_percent -= 25
            self.log.append(f'{actor.name} 的水位達到 70：【窒息海域】使全隊受到的治療永久降低 25%。')

        if self.mechanics.get('whale_swallow_charging'):
            self.mechanics['whale_swallow_charging'] = False
            self.mechanics['whale_next_swallow'] = self.round + 3
            self.record_skill(actor, '吞城')
            self.log.append(f'{actor.name} 釋放【吞城】：對全體造成 200% 傷害！')
            for enemy in self.living(0):
                if actor.hp > 0 and enemy.hp > 0:
                    self.hit(actor, enemy, 2.0, attack_scope='group')
            return
        if tide == 100 and self.round >= self.mechanics.get('whale_next_swallow', self.round):
            self.mechanics['whale_swallow_charging'] = True
            self.record_skill(actor, '吞城蓄力')
            self.log.append(f'{actor.name} 正在蓄力【吞城】，下一次行動將對全體造成 200% 傷害！')
            return

        if self.round % 2:
            self.record_skill(actor, '傾城撞擊')
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            self.log.append(f'{actor.name} 使用【傾城撞擊】：造成 130% 單體傷害！')
            if target is not None:
                self.hit(actor, target, 1.3)
            return
        power = 0.6 * (1.15 if self.mechanics.get('whale_flooded_streets') else 1)
        self.record_skill(actor, '巨浪擺尾')
        self.log.append(f'{actor.name} 使用【巨浪擺尾】：對全體造成 {power * 100:g}% 傷害！')
        for enemy in self.living(0):
            if actor.hp > 0 and enemy.hp > 0:
                self.hit(actor, enemy, power, attack_scope='group')

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

    def furnace_act(self, actor):
        if self.mechanics.get('furnace_charging'):
            self.mechanics['furnace_charging'] = False
            layers = self.mechanics.get('furnace_armor', 0)
            if not layers:
                actor.effects['break'] = self.round + 1
                self.log.append(f'{actor.name} 的爐甲已經崩解，【爐心震爆】取消。')
                return
            power = 1.8 + layers * .2
            self.record_skill(actor, '爐心震爆')
            self.log.append(f'{actor.name} 釋放【爐心震爆】：剩餘 {layers} 層爐甲，全體 {power * 100:g}% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, power, attack_scope='group')
            return
        if self.round % 4 == 0:
            self.mechanics.update(furnace_armor=3, furnace_charging=True)
            self.record_skill(actor, '重燃爐甲')
            self.log.append(f'{actor.name} 使用【重燃爐甲】：爐甲恢復至 3 層，下一回合將釋放爐心震爆！')
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None:
            self.hit(actor, target)

    def fungus_act(self, actor):
        if actor.job == '爆裂孢子':
            started = actor.status_stacks.get('spore_swelling_round')
            if started is not None and started < self.round and actor.has('spore_swelling', self.round):
                self.record_skill(actor, '孢子爆裂')
                self.log.append(f'{actor.name} 釋放【孢子爆裂】：對全隊造成 70% 傷害並附加中毒！')
                for enemy in self.living(0):
                    if enemy.hp > 0 and self.hit(actor, enemy, .7, attack_scope='group') and enemy.hp > 0:
                        self.apply_debuff(enemy, 'poison', self.round + 2, actor)
                actor.hp = 0
                actor.combat_stats['deaths'] += 1
                return
            if self.round % 3 == 0:
                actor.status_stacks['spore_swelling_round'] = self.round
                actor.effects.update(spore_swelling=self.round + 1,
                                     charging=self.round + 1, mechanic_target=self.round + 1)
                self.record_skill(actor, '膨脹')
                self.log.append(f'{actor.name} 開始【膨脹】，下一回合將爆裂！')
                return
            target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
            self.record_skill(actor, '孢子撞擊')
            self.log.append(f'{actor.name} 使用【孢子撞擊】')
            if target is not None:
                self.hit(actor, target, .45)
            return
        if self.round % 4 == 0:
            self.record_skill(actor, '菌霧滋養')
            amounts = []
            for spore in [f for f in self.living(1) if f.job == '爆裂孢子']:
                amounts.append(self.restore(spore, spore.stats['HP'] // 10))
            self.log.append(f'{actor.name} 使用【菌霧滋養】，存活孢子共恢復 {sum(amounts)} HP。')
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None:
            self.hit(actor, target)

    def star_act(self, actor):
        if actor.job == '蝕光星核':
            self.record_skill(actor, '星核脈動')
            self.log.append(f'{actor.name} 維持【蝕光屏障】，本回合不攻擊。')
            return
        core = next((f for f in self.living(1) if f.job == '蝕光星核'), None)
        if self.mechanics.get('star_charging'):
            self.mechanics['star_charging'] = False
            if core is None:
                actor.effects['break'] = self.round + 1
                self.log.append(f'{actor.name} 失去星核，【星蝕墜落】取消。')
                return
            self.record_skill(actor, '星蝕墜落')
            self.log.append(f'{actor.name} 釋放【星蝕墜落】：對全隊造成 200% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 2.0, attack_scope='group')
            return
        if self.round % 4 == 3:
            if core is None:
                fallen = next((f for f in self.fighters if f.job == '蝕光星核'), None)
                down_round = self.mechanics.get('star_core_down_round', -99)
                if fallen is not None and self.round - down_round >= 3:
                    fallen.hp = fallen.stats['HP']
                    fallen.combat_stats['deaths'] = max(0, fallen.combat_stats['deaths'] - 1)
                    fallen.status_stacks.pop('mechanic_defeat_processed', None)
                    core = fallen
                    self.log.append(f'{actor.name} 重建【蝕光星核】。')
            if core is not None:
                self.mechanics['star_charging'] = True
                self.record_skill(actor, '聚引星蝕')
                self.log.append(f'{actor.name} 使用【聚引星蝕】，下一回合將釋放星蝕墜落！')
                return
        if self.round % 5 == 0:
            self.record_skill(actor, '星塵橫掃')
            self.log.append(f'{actor.name} 使用【星塵橫掃】：對全隊造成 120% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 1.2, attack_scope='group')
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None:
            self.hit(actor, target)

    def add_drowning_mark(self, target):
        if target.has('immunity', self.round):
            self.log.append(f'{target.name} 受到【護衛】保護，免疫溺印。')
            return
        before = target.status_stacks.get('drowning_mark', 0)
        target.status_stacks['drowning_mark'] = min(3, before + 1)
        self.log.append(f'{target.name} 的溺印變為 {target.status_stacks["drowning_mark"]}/3 層。')

    def tide_act(self, actor):
        if actor.job == '逆潮法陣':
            self.record_skill(actor, '逆潮爆發')
            self.log.append(f'{actor.name} 完成【逆潮爆發】：對全隊造成 130% 傷害！')
            for enemy in self.living(0):
                if enemy.hp > 0:
                    self.hit(actor, enemy, 1.3, attack_scope='group')
            saint = next((f for f in self.living(1) if f.job == '逆潮聖骸'), None)
            if saint is not None:
                amount = self.restore(saint, saint.stats['HP'] * 8 // 100)
                self.log.append(f'{saint.name} 受到逆潮滋養，恢復 {amount} HP。')
            actor.status_stacks['mechanic_defeat_processed'] = 1
            actor.hp = 0
            actor.combat_stats['deaths'] += 1
            return
        circle = next((f for f in self.living(1) if f.job == '逆潮法陣'), None)
        if self.round % 3 == 0 and circle is None:
            circle_stats = dict(actor.stats)
            circle_stats.update(HP=max(1, actor.stats['HP'] * 12 // 100),
                                防禦=max(1, actor.stats['防禦'] * 65 // 100), 暴擊率=0)
            circle = Fighter(f'{actor.name}・逆潮法陣', 1, '逆潮法陣', circle_stats, 1, [],
                             is_boss=False, mechanic_priority=1)
            circle.effects.update(charging=self.round + 1, mechanic_target=self.round + 1)
            self.fighters.append(circle)
            self.record_skill(actor, '召喚逆潮法陣')
            self.log.append(f'{actor.name} 召喚【逆潮法陣】，下一回合將爆發！')
            return
        if self.round % 5 == 0:
            self.record_skill(actor, '溺潮')
            self.log.append(f'{actor.name} 使用【溺潮】：對全隊造成 80% 傷害並附加溺印！')
            for enemy in self.living(0):
                if enemy.hp > 0 and self.hit(actor, enemy, .8, attack_scope='group') and enemy.hp > 0:
                    self.add_drowning_mark(enemy)
            return
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        if target is not None and self.hit(actor, target) and target.hp > 0:
            self.add_drowning_mark(target)

    def noah_act(self, actor):
        """Resolve Noah's announced colour and the below-70% composition loop."""
        if self.mechanics.get('maze_final_phase') == 3:
            if self.mechanics.get('noah_final_charging'):
                self.mechanics['noah_final_charging'] = False
                self.record_skill(actor, '最後一筆')
                self.log.append(f'{actor.name} 完成【最後一筆】：對全隊造成 190% 傷害！')
                for enemy in self.living(0):
                    self.hit(actor, enemy, 1.9, attack_scope='group')
                return
            cycle = self.mechanics.get('noah_final_cycle', 0) + 1
            self.mechanics['noah_final_cycle'] = cycle
            charge_every = (2 if self.mechanics.get('maze_final_contracts', {}).get('gold', 0) >= 2
                            else 3)
            if cycle % charge_every == 0:
                self.mechanics['noah_final_charging'] = True
                self.log.append(f'{actor.name} 正在蓄力【最後一筆】；下一次行動前可打斷。')
                return
            if cycle % 2:
                target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
                self.log.append(f'{actor.name} 使用【完稿重描】。')
                if target:
                    self.hit(actor, target, 1.25)
            else:
                self.log.append(f'{actor.name} 使用【全幅侵蝕】。')
                for enemy in self.living(0):
                    self.hit(actor, enemy, .7, attack_scope='group')
            return
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
        if phase_two and self.mechanics.get('maze_final'):
            self.shadow_act(actor)
            composition = self.mechanics.get('noah_composition', 0) + 1
            self.mechanics['noah_composition'] = composition
            required = (2 if self.mechanics.get('maze_final_contracts', {}).get('gold', 0) >= 2
                        else 3)
            self.log.append(f'{actor.name} 的【源色構圖】進度 {composition}/{required}。')
            if composition >= required:
                self.mechanics['noah_composition'] = 0
                self.mechanics['noah_draft_charging'] = True
                self.log.append(f'{actor.name} 正在替【未完成稿】收尾。')
            return
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
                    if self.apply_debuff(target, 'break', self.round + 1, actor):
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
            required = (2 if self.mechanics.get('maze_final_contracts', {}).get('gold', 0) >= 2
                        else 3)
            self.log.append(f'{actor.name} 的【未完成構圖】進度 {composition}/{required}。')
            if composition >= required:
                self.mechanics['noah_draft_charging'] = True
                self.log.append(f'{actor.name} 正在替【未完成稿】收尾；下一次行動將對全隊造成 180% 傷害！')

    def shadow_act(self, actor):
        skills = self.mechanics.get('maze_final_skills') or ['black']
        index = self.mechanics.get('maze_shadow_skill_index', 0)
        color = skills[index % len(skills)]
        self.mechanics['maze_shadow_skill_index'] = index + 1
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        if color == 'crimson':
            self.log.append(f'{actor.name} 使用【緋紅影擊】。')
            self.hit(actor, target, 1.4)
        elif color == 'azure':
            shield = max(1, actor.stats['HP'] * 2 // 100)
            self.mechanics['maze_final_shield'] += shield
            self.log.append(f'{actor.name} 展開【蒼藍影幕】 {shield} 點。')
        elif color == 'gold':
            self.log.append(f'{actor.name} 使用【金黃連寫】。')
            for enemy in self.living(0):
                self.hit(actor, enemy, .6, attack_scope='group')
        elif color == 'verdant':
            amount = self.restore(actor, actor.stats['HP'] // 200)
            self.log.append(f'{actor.name} 使用【翠綠回影】，恢復 {amount} HP。')
        elif color == 'violet':
            self.log.append(f'{actor.name} 使用【紫蝕波紋】。')
            for enemy in self.living(0):
                self.hit(actor, enemy, .55, attack_scope='group')
        else:
            self.log.append(f'{actor.name} 使用【漆黑壓筆】。')
            self.hit(actor, target, 1.1)

    def act(self, actor):
        if actor.team == 1 and actor.job == '熔爐鎧獸':
            self.furnace_act(actor)
            return
        if actor.team == 1 and actor.job in ('迷霧菌后', '爆裂孢子'):
            self.fungus_act(actor)
            return
        if actor.team == 1 and actor.job in ('星蝕巨神', '蝕光星核'):
            self.star_act(actor)
            return
        if actor.team == 1 and actor.job in ('逆潮聖骸', '逆潮法陣'):
            self.tide_act(actor)
            return
        if actor.team == 1 and actor.job in ('赤雷', '蒼炎'):
            self.twin_act(actor)
            return
        if actor.team == 1 and actor.job == '吞城鯨':
            self.whale_act(actor)
            return
        if actor.team == 1 and actor.job == '城崎諾亞':
            if self.mechanics.get('maze_final_route') == 'shadow':
                self.shadow_act(actor)
                return
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
                if self.apply_debuff(target, 'break', self.round + 1, actor):
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
                if self.apply_debuff(target, 'stun', self.round + 1, actor):
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
                if actor.hp <= 0:
                    break
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
            target = self.target(actor, self.living(1 - actor.team),
                                 Rule(0, 0, True, 'always', actor.basic_target), True)
            self.record_skill(actor, '普通攻擊')
            self.log.append(f'{actor.name} 使用普通攻擊')
            hit = self.basic_attack(actor, target)
            if hit and actor.job == '毒蛛' and target.hp > 0:
                self.apply_debuff(target, 'poison', self.round + 2, actor)
            if hit and actor.job == '瘟疫縫合獸' and target.hp > 0:
                self.add_corruption(target)
            return
        rule, skill, target = selected
        self.use_skill(actor, rule, skill, target)

    def use_skill(self, actor, rule, skill, target):
        context = self._begin_passive_action(actor, skill, target)
        try:
            return self._resolve_skill(actor, rule, skill, target)
        finally:
            self._finish_passive_action(context)

    def _resolve_skill(self, actor, rule, skill, target):
        """Resolve an already-selected skill.

        Automatic raids choose the skill through ``select``; interactive battle
        modes can call this method after validating a player's explicit choice.
        """
        self.record_skill(actor, skill.name)
        cooldown = max(1, skill.cooldown - actor.cooldown_reduction)
        if (actor.first_skill_cooldown_reduction and not actor.first_skill_cooldown_used
                and skill.cooldown >= 2):
            cooldown = max(1, cooldown - actor.first_skill_cooldown_reduction)
            actor.first_skill_cooldown_used = True
            self.log.append(f'{actor.name} 的【循環徽記】使【{skill.name}】冷卻減少 '
                            f'{actor.first_skill_cooldown_reduction} 回合。')
        actor.ready[rule.slot] = self.round + cooldown + 1
        self.log.append(f'{actor.name} 使用【{skill.name}】')
        effect = skill.effect
        if effect in ('group_heal', 'rally'):
            targets = self.living(actor.team) if effect == 'group_heal' else [actor]
            healing = actor.stats['治療量'] * 65 // 100 if effect == 'group_heal' else actor.stats['HP'] // 2
            for ally in targets:
                amount = self.heal(actor, ally, healing)
                self.log.append(f'{ally.name} 恢復 {amount} HP')
        elif effect == 'heal':
            healing = actor.stats['治療量'] // 2 if actor.job == '民兵' else actor.stats['治療量']
            amount = self.heal(actor, target, healing)
            self.log.append(f'{target.name} 恢復 {amount} HP')
        elif effect == 'holy_light':
            for enemy in self.living(1 - actor.team):
                if actor.hp > 0 and enemy.hp > 0:
                    self.hit(actor, enemy, 0.9, attack_scope='group')
            amount = self.heal(actor, target, actor.stats['治療量'] * 70 // 100)
            self.log.append(f'{target.name} 恢復 {amount} HP')
        elif effect == 'cleanse':
            removed = self.clear_negative_effects(target)
            removed += bool(target.status_stacks.get('source_erosion'))
            if self._passive_action is not None:
                self._passive_action['cleansed'] = removed
            cleanse_heal = actor.status_stacks.get('crystal_cleanse_heal_percent', 0)
            if removed and cleanse_heal:
                amount = self.heal(actor, target, target.stats['HP'] * cleanse_heal // 100,
                                   passive_trigger=False)
                self.log.append(f'{target.name} 因【洗彩療癒】恢復 {amount} HP。')
            self.log.append(f'移除 {target.name} 的負面狀態')
        elif effect == 'guard':
            bonus = max(1, actor.stats['防禦'])
            for ally in self.living(actor.team):
                if not ally.has('guard', self.round) or ally.guard_bonus < bonus:
                    ally.guard_bonus = bonus
                    ally.effects['guard'] = self.round + 1
                    ally.effects['immunity'] = self.round + 1
                    if actor.user_id is not None:
                        ally.effect_sources['guard'] = actor.user_id
                    self.clear_negative_effects(ally)
                    self.log.append(f'{ally.name} 防禦 +{bonus} 並免疫負面狀態至第 {self.round + 1} 回合結束')
        elif effect in ('bless', 'stance', 'taunt'):
            target.effects[effect] = self.round + 1
            if effect == 'bless':
                target.effect_sources['bless'] = actor.user_id
                if self._passive_action is not None:
                    self._passive_action['hymn_bless'] = True
            self.log.append(f'{target.name} 獲得效果，持續至第 {self.round + 1} 回合結束')
        elif effect == 'knight_charge':
            self.hit(actor, target, 0.5, attack_override=actor.stats['HP'])
        elif effect in ('area', 'cleave'):
            for enemy in self.living(1 - actor.team):
                if actor.hp > 0 and enemy.hp > 0:
                    for _ in range(1 if effect == 'cleave' else 3):
                        if actor.hp > 0 and enemy.hp > 0:
                            self.hit(actor, enemy, 1.2 if effect == 'cleave' else 0.4,
                                     attack_scope='group')
        elif effect in ('double', 'triple'):
            for _ in range(3 if effect == 'triple' else 2):
                if actor.hp > 0 and target.hp > 0:
                    self.hit(actor, target, 0.85 if effect == 'triple' else 0.9)
        else:
            power = {'strike': 1.6, 'hindering_shot': 1.2, 'crush': 2.2,
                     'shield_bash': 1.2, 'poison_arrow': 1.1}.get(effect, 1)
            hit = self.hit(actor, target, power)
            if hit and effect == 'break' and target.hp > 0:
                self.apply_debuff(target, 'break', self.round + 1, actor)
            if hit and effect == 'hindering_shot' and target.hp > 0:
                if self.apply_debuff(target, 'weak', self.round + 1, actor):
                    self.log.append(f'{target.name} 陷入虛弱，攻擊降低 20%。')
            if hit and effect == 'poison_arrow' and target.hp > 0:
                if target.job == '逆潮法陣':
                    self.log.append(f'{target.name} 免疫毒箭侵蝕。')
                elif target.has('immunity', self.round):
                    self.log.append(f'{target.name} 受到【護衛】保護，免疫毒箭侵蝕。')
                else:
                    attack = actor.stats['攻擊']
                    attack *= 1.2 if actor.job == '裝甲步兵' and actor.has('stance', self.round) else 1
                    attack *= 0.8 if actor.has('weak', self.round) else 1
                    attack *= (self.damage_dealt_multiplier(actor)
                               * self.debuff_damage_multiplier(actor, target))
                    attack *= 1.25 if actor.has('bless', self.round) else 1
                    duration = 2 + (actor.status_stacks.get('maze_debuff_duration_bonus', 0)
                                    if actor.team == 0 and target.team != actor.team else 0)
                    target.status_stacks.setdefault('poison_arrows', []).append({
                        'source_id': actor.user_id, 'damage': max(1, int(attack * 0.7)),
                        'next_round': self.round + 1, 'remaining': duration,
                    })
                    self.log.append(f'{target.name} 遭毒箭侵蝕，後續 {duration} 回合將受到無視防禦傷害。')
            if hit and target.hp > 0 and effect == 'shield_bash':
                if self._passive_action is not None and self._passive_action.get('shield_followup'):
                    self.log.append(f'{actor.name} 消耗衝勢，盾擊追加 40% 傷害。')
                    self.hit(actor, target, .4, counterable=False, passive_trigger=False)
                status = 'stun'
                if (target.job in ('赤雷', '蒼炎') and self.mechanics.get('twin_revive_job')
                        and not self.mechanics.get('twin_revive_delayed')):
                    self.mechanics['twin_revive_round'] += 1
                    self.mechanics['twin_revive_delayed'] = True
                    self.log.append(f'{target.name} 的【再生共鳴】受到干擾，復活延後一回合。')
                    return
                if target.job == '吞城鯨' and self.mechanics.get('whale_swallow_charging'):
                    self.mechanics['whale_swallow_charging'] = False
                    self.mechanics['whale_next_swallow'] = self.round + 3
                    target.effects['break'] = self.round + 1
                    self.log.append(f'{target.name} 的【吞城】被打斷，遭到破甲至第 {self.round + 1} 回合結束。')
                    return
                if target.job == '熔爐鎧獸' and self.mechanics.get('furnace_charging'):
                    self.mechanics['furnace_charging'] = False
                    self.mechanics['furnace_armor'] = 0
                    target.effects['break'] = self.round + 1
                    self.log.append(f'{target.name} 的【爐心震爆】被打斷，爐甲崩解並破甲至第 {self.round + 1} 回合結束。')
                    return
                if target.job == '星蝕巨神' and self.mechanics.get('star_charging'):
                    self.mechanics['star_charging'] = False
                    target.effects['break'] = self.round + 1
                    self.log.append(f'{target.name} 的【星蝕墜落】被盾擊打斷，破甲至第 {self.round + 1} 回合結束。')
                    return
                if target.job == '爆裂孢子' and target.has('spore_swelling', self.round):
                    for effect_name in ('spore_swelling', 'charging', 'mechanic_target'):
                        target.effects.pop(effect_name, None)
                    self.log.append(f'{target.name} 的膨脹被打斷，恢復一般行動。')
                    return
                if target.job == '逆潮法陣':
                    self.log.append(f'{target.name} 的法陣蓄力無法被盾擊打斷，必須將其擊倒。')
                    return
                if status == 'stun' and target.job == '深淵鐘龍':
                    self.log.append(f'{target.name} 免疫暈眩，鐘甲不會被盾擊直接打斷。')
                    return
                if target.job == '繪畫魔女．城崎諾亞':
                    self.log.append(f'{target.name} 免疫暈眩；只有黑色能使她停止行動。')
                    return
                if target.job == '城崎諾亞':
                    if status == 'stun' and (self.mechanics.get('noah_draft_charging')
                                             or self.mechanics.get('noah_final_charging')):
                        final = self.mechanics.get('noah_final_charging')
                        self.mechanics.update(noah_draft_charging=False,
                                              noah_final_charging=False,
                                              noah_composition=0, noah_color_index=0)
                        target.effects['break'] = self.round + 1
                        name = '最後一筆' if final else '未完成稿'
                        self.log.append(f'{target.name} 的【{name}】被打斷；構圖歸零並遭破甲至第 {self.round + 1} 回合結束。')
                    else:
                        self.log.append(f'{target.name} 免疫暈眩；盾擊只能在未完成稿蓄力時打斷構圖。')
                    return
                if target.has('immunity', self.round):
                    self.log.append(f'{target.name} 受到【護衛】保護，免疫暈眩。')
                    return
                if status == 'stun' and target.effects.pop('charged_punch', None) is not None:
                    self.log.append(f'{target.name} 的蓄力被打斷')
                self.apply_debuff(target, status, self.round + 1, actor)
                self.log.append(f'{target.name} 暈眩')

    def step(self):
        if self.result or self.check_end():
            return
        self.round += 1
        self.log.append(f'── 第 {self.round} 回合 ──')
        self.process_twin_revival()
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
            if actor.status_stacks.get('poison_arrows') and not self.tick_poison_arrows(actor):
                if self.check_end():
                    break
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
            if actor.job in ('城崎諾亞', '繪畫魔女．城崎諾亞') and actor.effects.pop('stun', None) is not None:
                self.log.append(f'{actor.name} 免疫暈眩，沒有跳過行動。')
            if actor.has('stun', self.round):
                actor.effects.pop('stun', None)
                self.log.append(f'{actor.name} 因暈眩跳過本次行動。')
                continue
            self.act(actor)
            if self.check_end():
                break
        self._maze_final_round_end()
        self._maze_party_round_end()
        maze_traits.round_end(self)
        if not self.result:
            for fighter in list(self.living(0)):
                marks = fighter.status_stacks.get('drowning_mark', 0)
                if not marks:
                    continue
                damage = max(1, fighter.stats['HP'] * marks * 2 // 100)
                actual, partner, partner_actual = self.apply_damage(fighter, damage)
                self.log.append(f'{fighter.name} 的 {marks} 層【溺印】造成 {actual + partner_actual} HP 傷害。')
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
                        basic_target=p.get('basic_target', 'lowest'),
                        stability=tuple(p['state'].get('stability', (100, 100))),
                        lifesteal=p['state'].get('lifesteal', 0),
                        healing_received_percent=p['state'].get('healing_received_percent', 0),
                        damage_guard_chance=p['state'].get('damage_guard_chance', 0),
                        vulnerable_chance=p['state'].get('vulnerable_chance', 0),
                        vulnerable_percent=p['state'].get('vulnerable_percent', 0),
                        healing_share=p['state'].get('healing_share', 0),
                        alternating_damage_percent=p['state'].get('alternating_damage_percent', 0),
                        defense_conversion=p['state'].get('defense_conversion', False),
                        first_skill_cooldown_reduction=p['state'].get(
                            'first_skill_cooldown_reduction', 0),
                        critical_damage_percent=p['state'].get('critical_damage_percent', 150),
                        armed=bool(p['state'].get('equipped', {}).get('武器')),
                        passive_id=p.get('passive_id'),
                        user_id=p.get('id')) for p in participants]
    for fighter, participant in zip(fighters, participants):
        for embroidery in participant['state'].get('embroideries', ()):
            fighter.status_stacks['embroidery_' + embroidery] = 1
        for crystal in participant['state'].get('crystal_effects', ()):
            for effect, value in zip(crystal.get('effects', ()), crystal.get('values', ())):
                if effect in ('HP', '攻擊', '防禦', '治療量', 'accuracy',
                              'critical_points', 'evasion_points', 'speed',
                              'lifesteal_percent', 'healing_received_percent'):
                    continue
                fighter.status_stacks[f'crystal_{effect}'] = value
        shield = fighter.status_stacks.get('crystal_opening_shield_percent', 0)
        if shield:
            fighter.status_stacks['crystal_shield'] = fighter.stats['HP'] * shield // 100
        if fighter.status_stacks.get('crystal_survive_fatal_once'):
            fighter.status_stacks['crystal_survive_fatal_once'] = 1
        if fighter.status_stacks.get('crystal_immovable_wall'):
            fighter.passive_state['crystal_wall'] = 3
    badge_logs = []
    passive_logs = []
    for fighter in fighters:
        selected = next((passive for passive in PASSIVES.get(fighter.job, ())
                         if passive.id == fighter.passive_id), None)
        if selected:
            passive_logs.append(f'{fighter.name} 裝備職業被動【{selected.name}】。')
    for fighter, participant in zip(fighters, participants):
        if participant['state'].get('set_bonus_text'):
            passive_logs.append(f'{fighter.name} 啟動套裝【{participant["state"]["set_bonus_text"]}】。')
        if participant['state'].get('first_skill_cooldown_reduction'):
            passive_logs.append(f'{fighter.name} 裝備【循環徽記】，第一次符合資格的主動技能冷卻將減少 1 回合。')
        if any(ITEMS[key].party_bonus for key in participant['state'].get('equipped', {}).values() if key in ITEMS):
            count = min(5, (len(participants) + 1) // 2)
            total = participant['state']['total']
            before = combat_from_stats(total, fighter.job)
            after = combat_from_stats([value + count for value in total], fighter.job)
            for stat in before:
                fighter.stats[stat] += after[stat] - before[stat]
            fighter.hp = fighter.stats['HP']
            badge_logs.append(f'{fighter.name} 的【戰團徽章】生效：{len(participants)} 人參戰，生命力／力氣／耐力／靈巧／信仰各 +{count}，整場固定。')
    provision_logs = []
    stat_caps = {'閃避率': 40, '暴擊率': 100}

    def percent_stat(fighter, stat, percent):
        before = fighter.stats[stat]
        fighter.stats[stat] = max(1 if stat == 'HP' else 0, before * (100 + percent) // 100)
        if stat == 'HP':
            fighter.hp = fighter.stats['HP']

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
        meal = participant.get('meal') or {}
        if meal:
            if meal.get('hp_percent'):
                percent_stat(fighter, 'HP', meal['hp_percent'])
            if meal.get('attack_percent'):
                percent_stat(fighter, '攻擊', meal['attack_percent'])
            if meal.get('healing_percent'):
                percent_stat(fighter, '治療量', meal['healing_percent'])
            fighter.stats['暴擊率'] = min(
                100, fighter.stats['暴擊率'] + meal.get('critical_points', 0))
            fighter.lifesteal += meal.get('lifesteal_percent', 0)
            provision_logs.append(
                f'{fighter.name} 享用【{meal.get("name", "酒館料理")}】，料理效果整場生效。')
    fortune_logs = []
    fortune_rng = random.Random(f'{seed}:divination')

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
            fighter.stats['命中率'] += 3
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
            fighter.stats['暴擊率'] = min(100, fighter.stats['暴擊率'] + 5)
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
        fighter_speed = speed
        if monster['kind'] == '王城傀儡師':
            shares = (50, 25, 25)
            individual['HP'] = max(1, stats['HP'] * shares[i] // 100
                                   + (stats['HP'] - sum(stats['HP'] * share // 100 for share in shares) if i == 0 else 0))
        elif monster['kind'] == '迷霧菌后':
            shares = (60, 20, 20)
            individual['HP'] = max(1, stats['HP'] * shares[i] // 100
                                   + (stats['HP'] - sum(stats['HP'] * share // 100 for share in shares) if i == 0 else 0))
        elif monster['kind'] == '星蝕巨神':
            shares = (80, 20)
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
        if monster['kind'] == '赤雷與蒼炎':
            job = ('赤雷', '蒼炎')[i]
            name = f'{monster_name(monster)}・{job}'
        if monster['kind'] == '迷霧菌后':
            job = '迷霧菌后' if i == 0 else '爆裂孢子'
            name = monster_name(monster) if i == 0 else f'{monster_name(monster)}・爆裂孢子{i}'
            if i:
                individual['防禦'] = max(1, int((10 + average * 2) * 1.0))
                individual['閃避率'] = 60
                individual['暴擊率'] = 5
                fighter_speed = 58
        if monster['kind'] == '星蝕巨神':
            job = '星蝕巨神' if i == 0 else '蝕光星核'
            name = monster_name(monster) if i == 0 else f'{monster_name(monster)}・蝕光星核'
            if i:
                individual['攻擊'] = 0
                individual['防禦'] = max(1, int((10 + average * 2) * 1.2))
                individual['命中率'] = 0
                individual['閃避率'] = 45
                individual['暴擊率'] = 0
                fighter_speed = 1
        is_boss = (count == 1 or monster['kind'] == '赤雷與蒼炎' or
                   monster['kind'] == '哥布林戰團' and i == 0 or
                   monster['kind'] == '王城傀儡師' and i == 0 or
                   monster['kind'] in ('迷霧菌后', '星蝕巨神') and i == 0)
        mechanic_priority = (1 if monster['kind'] == '哥布林戰團' and i == 0 or
                             monster['kind'] == '王城傀儡師' and i > 0 or
                             monster['kind'] == '星蝕巨神' and i == 1 else 0)
        fighters.append(Fighter(name, 1, job, individual, fighter_speed, [],
                                is_boss=is_boss, mechanic_priority=mechanic_priority))
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
    if monster['kind'] == '赤雷與蒼炎':
        twins = fighters[-2:]
        battle.mechanics.update(
            twin_base_attacks={fighter.job: fighter.stats['攻擊'] for fighter in twins},
            twin_base_speeds={fighter.job: fighter.speed for fighter in twins})
    if monster['kind'] == '吞城鯨':
        battle.mechanics.update(whale_shield=min(10, 3 + len(participants) // 3),
                                whale_tide=0, whale_next_swallow=10)
    if monster['kind'] == '熔爐鎧獸':
        battle.mechanics.update(furnace_armor=3, furnace_charging=False)
    if monster['kind'] == '星蝕巨神':
        battle.mechanics.update(star_charging=False, star_core_down_round=-99)
    battle.log.extend(badge_logs)
    battle.log.extend(passive_logs)
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
        f.basic_target = data_f.get('basic_target', 'lowest')
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
        f.alternating_damage_percent = data_f.get('alternating_damage_percent', 0)
        f.defense_conversion = data_f.get('defense_conversion', False)
        f.stored_defense_attack = data_f.get('stored_defense_attack', 0)
        # Battles saved before critical damage became a fighter stat used 150%.
        f.critical_damage_percent = data_f.get('critical_damage_percent', 150)
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
        f.first_skill_cooldown_reduction = data_f.get('first_skill_cooldown_reduction', 0)
        f.first_skill_cooldown_used = data_f.get('first_skill_cooldown_used', False)
        f.linked_user_id = data_f.get('linked_user_id')
        f.passive_id = data_f.get('passive_id')
        f.passive_state = data_f.get('passive_state', {})
        f.is_boss = data_f.get('is_boss', False)
        f.mechanic_priority = data_f.get('mechanic_priority', 0)
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
