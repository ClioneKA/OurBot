"""Deterministic when seeded: automatic turn combat and persistent skill tactics."""
from dataclasses import dataclass, field
from decimal import Decimal
import random

from core.rpg_character import CharacterError, ITEMS, combat_from_stats
from core.rpg_monsters import monster_name
from core.rpg import level_for


CONDITIONS = {'always': '可用就施放', 'self40': '自身 HP ≤ 40%',
              'ally50': '隊伍有人 HP ≤ 50%', 'enemies3': '存活敵人 ≥ 3',
              'ally_debuff': '隊友有可淨化負面狀態（含自己）'}
TARGETS = {'lowest': '血量比例最低', 'strongest': '攻擊最高', 'self': '自己',
           'debuffed': '有可淨化負面狀態的隊友'}


def empty_combat_stats():
    """Per-fighter counters kept in battle snapshots for settlement and analysis."""
    return dict(damage_dealt=0, damage_taken=0, healing_done=0, healing_received=0,
                overhealing=0, attacks=0, hits=0, misses=0, critical_hits=0,
                knockouts=0, deaths=0, skills_used={})


@dataclass(frozen=True)
class Skill:
    name: str
    effect: str
    cooldown: int
    description: str
    condition: str = 'always'


SKILLS = {
    '民兵': (Skill('奮力一擊', 'strike', 2, '造成 160% 傷害'),
             Skill('包紮', 'heal', 3, '以治療量的 50% 恢復一名隊友生命', 'ally50'),
             Skill('防禦', 'stance', 3, '自身減傷 20%', 'self40')),
    '裝甲步兵': (Skill('重擊', 'strike', 2, '造成 160% 傷害'),
                 Skill('破甲', 'break', 3, '造成傷害並降低目標防禦 40%'),
                 Skill('攻守架勢', 'stance', 3, '自身減傷 35%、攻擊提升 20%', 'self40'),
                 Skill('橫掃斬', 'cleave', 4, '對全體敵人造成 120% 傷害'),
                 Skill('重裝猛擊', 'crush', 4, '對單一敵人造成 220% 傷害')),
    '騎士': (Skill('嘲諷', 'taunt', 3, '吸引敵方單體攻擊，自身減傷 15%'),
             Skill('護衛', 'guard', 3, '全隊防禦增加施放者最大 HP 的 5%，同效果取較強值', 'ally50'),
             Skill('堅守', 'stance', 3, '自身減傷 50%', 'self40'),
             Skill('盾擊', 'shield_bash', 4, '造成 120% 傷害，命中後暈眩至下一回合結束（跳過一次行動）'),
             Skill('重整旗鼓', 'rally', 4, '恢復自身最大 HP 的 25%', 'self40')),
    '弓兵': (Skill('連射', 'double', 2, '兩次 85% 傷害，各自判定命中'),
             Skill('精準射擊', 'precise', 3, '必中，造成 150% 傷害'),
             Skill('箭雨', 'area', 4, '對所有敵人造成 80% 傷害'),
             Skill('三連矢', 'triple', 4, '對單一敵人連射三次，每次 75% 傷害，分別判定命中'),
             Skill('毒箭', 'poison_arrow', 3, '造成 120% 傷害，命中後中毒至後兩回合結束；行動前損失最大 HP 的 2%（無條件捨去，最低 1）')),
    '僧侶': (Skill('治療', 'heal', 2, '恢復一名隊友生命', 'ally50'),
             Skill('祝福', 'bless', 3, '提升一名隊友攻擊 25%'),
             Skill('淨化', 'cleanse', 2, '移除一名隊友的中毒、破甲與暈眩', 'ally_debuff'),
             Skill('群體治療', 'group_heal', 4, '恢復全體存活隊友各 65% 治療量的 HP', 'ally50'),
             Skill('強效治療', 'greater_heal', 4, '恢復一名隊友 180% 治療量的 HP', 'ally50')),
}
ALLY_EFFECTS = {'heal', 'guard', 'bless', 'cleanse', 'group_heal', 'greater_heal'}
FIXED_TARGETS = {'guard': '全隊', 'group_heal': '全隊', 'area': '全體敵人',
                 'cleave': '全體敵人', 'stance': '自己', 'taunt': '自己', 'rally': '自己'}


def unlocked_skills(job, level):
    return SKILLS[job] if level >= 20 else SKILLS[job][:3]


def rule_skill(job, rule):
    # Old saved tactics and battle snapshots used the slot as the skill ID.
    return SKILLS[job][(rule.skill_id or rule.slot) - 1]


@dataclass(frozen=True)
class Rule:
    slot: int
    priority: int
    enabled: bool
    condition: str
    target: str
    skill_id: int | None = None


def default_rules(job):
    return [Rule(i, i, True, skill.condition, 'debuffed' if skill.effect == 'cleanse' else 'lowest')
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

    def available(self, guild, user, job):
        return unlocked_skills(job, level_for(self.store.xp(guild, user)))

    def rules(self, guild, user, job):
        saved = {row[0]: Rule(row[0], row[1], bool(row[2]), row[3], row[4], row[5]) for row in self.db.execute(
            'SELECT slot, priority, enabled, condition, target, skill_id FROM rpg_tactics '
            'WHERE guild_id=? AND user_id=? AND job=?', (guild, user, job))}
        return sorted([saved.get(rule.slot, rule) for rule in default_rules(job)], key=lambda rule: rule.priority)

    def configure(self, guild, user, job, slot, priority, enabled, condition, target):
        if job not in SKILLS or slot not in (1, 2, 3) or priority not in (1, 2, 3):
            raise CharacterError('技能槽與優先順序必須是 1–3。')
        if condition not in CONDITIONS or target not in TARGETS or type(enabled) is not bool:
            raise CharacterError('無效的自動施放設定。')
        rules = self.rules(guild, user, job)
        current = next(rule for rule in rules if rule.slot == slot)
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
                    rule = Rule(slot, priority, enabled, condition, target, rule.skill_id)
                elif rule.priority == priority:
                    rule = Rule(rule.slot, old_priority, rule.enabled, rule.condition, rule.target, rule.skill_id)
                updated.append((guild, user, job, rule.slot, rule.priority, int(rule.enabled), rule.condition, rule.target, rule.skill_id))
            self.db.executemany('INSERT OR REPLACE INTO rpg_tactics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', updated)

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
            self.db.execute('INSERT OR REPLACE INTO rpg_tactics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (guild, user, job, slot, current.priority, int(current.enabled), skill.condition,
                             'debuffed' if skill.effect == 'cleanse' else 'lowest', skill_id))


@dataclass
class Fighter:
    name: str
    team: int
    job: str
    stats: dict
    dexterity: int
    rules: list
    hp: int = field(init=False)
    ready: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
    effect_sources: dict = field(default_factory=dict)
    guard_bonus: int = 0
    stability: tuple = (100, 100)
    armed: bool = True
    lifesteal: int = 0
    user_id: int | None = None
    combat_stats: dict = field(default_factory=empty_combat_stats)
    food_name: str = ''
    food_heal_permille: int = 0
    food_regen_permille: int = 0
    food_regen_rounds: int = 0
    food_used: bool = False
    food_regen_left: int = 0
    food_regen_start: int = 0

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


class Battle:
    def __init__(self, fighters, seed=None, max_rounds=30):
        self.fighters = fighters
        self.rng = random.Random(seed)
        self.round = 0
        self.max_rounds = max_rounds
        self.result = None
        self.log = []

    @staticmethod
    def record_skill(actor, name):
        used = actor.combat_stats['skills_used']
        used[name] = used.get(name, 0) + 1

    @staticmethod
    def heal(actor, target, requested):
        requested = max(0, int(requested))
        amount = min(target.stats['HP'] - target.hp, requested)
        target.hp += amount
        actor.combat_stats['healing_done'] += amount
        actor.combat_stats['overhealing'] += requested - amount
        target.combat_stats['healing_received'] += amount
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

    def living(self, team):
        return [f for f in self.fighters if f.team == team and f.hp > 0]

    def check_end(self):
        if not self.living(0) and not self.living(1):
            self.result = '平手'
        elif not self.living(1):
            self.result = '勝利'
        elif not self.living(0):
            self.result = '戰敗'
        return self.result is not None

    def target(self, actor, candidates, rule, offensive=False):
        if rule.target == 'debuffed':
            candidates = [f for f in candidates if any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun'))]
        if offensive:
            taunters = [f for f in candidates if f.has('taunt', self.round)]
            candidates = taunters or candidates
        if rule.target == 'self':
            return actor if actor in candidates else None
        if not candidates:
            return None
        if rule.target == 'strongest':
            return max(candidates, key=lambda f: f.stats['攻擊'])
        return min(candidates, key=lambda f: f.hp / f.stats['HP'])

    def select(self, actor):
        allies, enemies = self.living(actor.team), self.living(1 - actor.team)
        for rule in sorted(actor.rules, key=lambda r: r.priority):
            skill = rule_skill(actor.job, rule)
            if not rule.enabled or self.round < actor.ready.get(rule.slot, 0):
                continue
            if rule.condition == 'self40' and actor.hp * 100 > actor.stats['HP'] * 40:
                continue
            if rule.condition == 'ally50' and not any(f.hp * 2 <= f.stats['HP'] for f in allies):
                continue
            if rule.condition == 'enemies3' and len(enemies) < 3:
                continue
            if rule.condition == 'ally_debuff' and not any(
                    any(f.has(effect, self.round) for effect in ('poison', 'break', 'stun')) for f in allies):
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
        chance = max(10, min(99, actor.stats['命中率'] - evasion))
        if not precise and self.rng.random() * 100 >= chance:
            actor.combat_stats['misses'] += 1
            self.log.append(f'{actor.name} → {target.name}：未命中')
            return False
        attack = actor.stats['攻擊']
        attack *= 1.25 if actor.has('bless', self.round) else 1
        attack *= 1.2 if actor.job == '裝甲步兵' and actor.has('stance', self.round) else 1
        defense = target.stats['防禦']
        if target.has('guard', self.round):
            defense += target.guard_bonus
        if target.has('break', self.round):
            defense *= 0.6
        damage = max(1, int(attack * power - defense * 0.35))
        low, high = actor.stability
        stability = self.rng.randint(low, high) if low != high else low
        damage = max(1, damage * stability // 100)
        critical = self.rng.random() * 100 < actor.stats['暴擊率']
        if critical:
            damage = int(damage * 1.5)
        if target.has('stance', self.round):
            multiplier = {'民兵': 0.8, '騎士': 0.5}.get(target.job, 0.65)
            damage = max(1, int(damage * multiplier))
        if target.has('taunt', self.round):
            damage = max(1, int(damage * 0.85))
        actual_damage = min(target.hp, damage)
        target.hp = max(0, target.hp - damage)
        actor.combat_stats['hits'] += 1
        actor.combat_stats['critical_hits'] += int(critical)
        actor.combat_stats['damage_dealt'] += actual_damage
        target.combat_stats['damage_taken'] += actual_damage
        if actual_damage and target.hp == 0:
            actor.combat_stats['knockouts'] += 1
            target.combat_stats['deaths'] += 1
        self.log.append(f'{actor.name} → {target.name}：{damage} 傷害{"（暴擊）" if critical else ""}'
                        f'{f"（穩定度 {stability}%）" if actor.stability != (100, 100) else ""}{"，倒下" if target.hp == 0 else ""}')
        self.maybe_eat(target)
        drain = actor.lifesteal if lifesteal is None else lifesteal
        healing = self.heal(actor, actor, actual_damage * drain // 100)
        if healing > 0 and actor.hp > 0:
            self.log.append(f'{actor.name} 吸血恢復 {healing} HP')
        return True

    def act(self, actor):
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
            return
        rule, skill, target = selected
        self.record_skill(actor, skill.name)
        actor.ready[rule.slot] = self.round + skill.cooldown + 1
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
            target.effect_sources.pop('poison', None)
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
            self.log.append(f'{target.name} 獲得效果，持續至第 {self.round + 1} 回合結束')
        elif effect in ('area', 'cleave'):
            for enemy in self.living(1 - actor.team):
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
            if hit and target.hp > 0 and effect in ('shield_bash', 'poison_arrow'):
                status = 'stun' if effect == 'shield_bash' else 'poison'
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
        self.rng.shuffle(order)  # Equal dexterity uses seeded random tie-breaking.
        order.sort(key=lambda f: f.dexterity, reverse=True)
        for actor in order:
            if actor.hp <= 0:
                continue
            if actor.has('poison', self.round):
                # PvE poison only targets the opposing team: monsters poison players
                # for 5%, while player poison arrows damage monsters for 2%.
                damage = max(1, actor.stats['HP'] // (20 if actor.team == 0 else 50))
                actual_damage = min(actor.hp, damage)
                actor.hp = max(0, actor.hp - damage)
                actor.combat_stats['damage_taken'] += actual_damage
                source_id = actor.effect_sources.get('poison')
                source = (next((fighter for fighter in self.fighters if fighter.user_id == source_id), None)
                          if source_id is not None else None)
                if source is not None and source is not actor:
                    source.combat_stats['damage_dealt'] += actual_damage
                if actual_damage and actor.hp == 0:
                    actor.combat_stats['deaths'] += 1
                    if source is not None and source is not actor:
                        source.combat_stats['knockouts'] += 1
                self.log.append(f'{actor.name} 中毒，損失 {damage} HP')
                self.maybe_eat(actor)
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
    fighters = [Fighter(p['name'], 0, p['state']['job'], dict(p['state']['combat']),
                        p['state']['total'][3], [Rule(**r) for r in p['rules']],
                        stability=tuple(p['state'].get('stability', (100, 100))),
                        lifesteal=p['state'].get('lifesteal', 0),
                        armed=bool(p['state'].get('equipped', {}).get('武器')),
                        user_id=p.get('id')) for p in participants]
    badge_logs = []
    for fighter, participant in zip(fighters, participants):
        if any(ITEMS[key].party_bonus for key in participant['state'].get('equipped', {}).values() if key in ITEMS):
            count = min(len(participants), 10)
            total = participant['state']['total']
            before = combat_from_stats(total)
            after = combat_from_stats([value + count for value in total])
            for stat in before:
                fighter.stats[stat] += after[stat] - before[stat]
            fighter.dexterity += count
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
    average = sum(p['state']['level'] for p in participants) / len(participants)
    stats = {'HP': int(sum(150 + p['state']['level'] * 28 for p in participants)),
             '攻擊': int(22 + average * 6), '防禦': int(10 + average * 2),
             '治療量': 0, '命中率': 92, '閃避率': 5, '暴擊率': 10}
    profile = monster.get('profile')
    speed = int(12 + average * 2)
    if profile:
        for stat, key in (('HP', 'hp'), ('攻擊', 'attack'), ('防禦', 'defense')):
            stats[stat] = max(1, int(stats[stat] * Decimal(str(profile[key]))))
        stats.update(命中率=profile['hit'], 閃避率=profile['dodge'], 暴擊率=profile['crit'])
        speed = max(1, int(speed * profile['speed']))
    elif monster['kind'] == '鐵殼魔像':
        stats['HP'] = int(stats['HP'] * 1.2)
        stats['防禦'] *= 2
    for stat in ('HP', '攻擊', '防禦'):
        stats[stat] = max(1, int(stats[stat] * monster.get('strength', 1)))
    count = profile['count'] if profile else 1
    for i in range(count):
        individual = dict(stats)
        individual['HP'] = max(1, stats['HP'] // count + (i < stats['HP'] % count))
        name = monster_name(monster) + (f'・{i + 1}' if count > 1 else '')
        job = '史萊姆' if count > 1 and monster['kind'] == '史萊姆群' else monster['kind']
        if monster['kind'] == '哥布林戰團':
            job = '哥布林隊長' if i == 0 else '哥布林打手'
            name = monster_name(monster) + ('・隊長' if i == 0 else f'・打手{i}')
        fighters.append(Fighter(name, 1, job, individual, speed, []))
    battle = Battle(fighters, seed=seed)
    battle.log.extend(badge_logs)
    battle.log.extend(provision_logs)
    return battle


def dump_battle(battle):
    from dataclasses import asdict
    return dict(fighters=[asdict(f) for f in battle.fighters], round=battle.round,
                max_rounds=battle.max_rounds, result=battle.result, log=battle.log,
                random_state=battle.rng.getstate())


def load_battle(data):
    def tuples(value):
        return tuple(tuples(v) for v in value) if isinstance(value, (tuple, list)) else value
    fighters = []
    for data_f in data['fighters']:
        f = Fighter(data_f['name'], data_f['team'], data_f['job'], data_f['stats'],
                    data_f['dexterity'], [Rule(**r) for r in data_f['rules']])
        f.hp = data_f['hp']
        f.ready = {int(k): v for k, v in data_f['ready'].items()}
        f.effects = data_f['effects']
        f.effect_sources = data_f.get('effect_sources', {})
        f.guard_bonus = data_f.get('guard_bonus', 0)
        f.stability = tuple(data_f.get('stability', (100, 100)))
        f.armed = data_f.get('armed', True)
        f.lifesteal = data_f.get('lifesteal', 0)
        f.user_id = data_f.get('user_id')
        f.food_name = data_f.get('food_name', '')
        f.food_heal_permille = data_f.get('food_heal_permille', 0)
        f.food_regen_permille = data_f.get('food_regen_permille', 0)
        f.food_regen_rounds = data_f.get('food_regen_rounds', 0)
        f.food_used = data_f.get('food_used', False)
        f.food_regen_left = data_f.get('food_regen_left', 0)
        f.food_regen_start = data_f.get('food_regen_start', 0)
        saved_stats = data_f.get('combat_stats', {})
        f.combat_stats = empty_combat_stats()
        for key in f.combat_stats:
            if key in saved_stats:
                f.combat_stats[key] = saved_stats[key]
        fighters.append(f)
    battle = Battle(fighters, max_rounds=data['max_rounds'])
    battle.round, battle.result, battle.log = data['round'], data['result'], list(data['log'])
    battle.rng.setstate(tuples(data['random_state']))
    return battle
