"""Deterministic when seeded: automatic turn combat and persistent skill tactics."""
from dataclasses import dataclass, field
import random

from core.rpg_character import CharacterError


CONDITIONS = {'always': '可用就施放', 'self40': '自身 HP ≤ 40%',
              'ally50': '隊伍有人 HP ≤ 50%', 'enemies3': '存活敵人 ≥ 3'}
TARGETS = {'lowest': '血量比例最低', 'strongest': '攻擊最高', 'self': '自己'}


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
                 Skill('攻守架勢', 'stance', 3, '自身減傷 35%、攻擊提升 20%', 'self40')),
    '騎士': (Skill('嘲諷', 'taunt', 3, '吸引敵方單體攻擊，自身減傷 15%'),
             Skill('護衛', 'guard', 3, '全隊防禦增加施放者最大 HP 的 5%，同效果取較強值', 'ally50'),
             Skill('堅守', 'stance', 3, '自身減傷 50%', 'self40')),
    '弓兵': (Skill('連射', 'double', 2, '兩次 85% 傷害，各自判定命中'),
             Skill('精準射擊', 'precise', 3, '必中，造成 150% 傷害'),
             Skill('箭雨', 'area', 4, '對所有敵人造成 80% 傷害')),
    '僧侶': (Skill('治療', 'heal', 2, '恢復一名隊友生命', 'ally50'),
             Skill('祝福', 'bless', 3, '提升一名隊友攻擊 25%'),
             Skill('淨化', 'cleanse', 2, '移除一名隊友的中毒與破甲')),
}
ALLY_EFFECTS = {'heal', 'guard', 'bless', 'cleanse'}


@dataclass(frozen=True)
class Rule:
    slot: int
    priority: int
    enabled: bool
    condition: str
    target: str


def default_rules(job):
    return [Rule(i, i, True, skill.condition, 'lowest') for i, skill in enumerate(SKILLS[job], 1)]


class Tactics:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_tactics (
                guild_id INTEGER, user_id INTEGER, job TEXT, slot INTEGER,
                priority INTEGER, enabled INTEGER, condition TEXT, target TEXT,
                PRIMARY KEY (guild_id, user_id, job, slot))''')

    def rules(self, guild, user, job):
        saved = {row[0]: Rule(row[0], row[1], bool(row[2]), row[3], row[4]) for row in self.db.execute(
            'SELECT slot, priority, enabled, condition, target FROM rpg_tactics '
            'WHERE guild_id=? AND user_id=? AND job=?', (guild, user, job))}
        return sorted([saved.get(rule.slot, rule) for rule in default_rules(job)], key=lambda rule: rule.priority)

    def configure(self, guild, user, job, slot, priority, enabled, condition, target):
        if job not in SKILLS or slot not in (1, 2, 3) or priority not in (1, 2, 3):
            raise CharacterError('技能槽與優先順序必須是 1–3。')
        if condition not in CONDITIONS or target not in TARGETS or type(enabled) is not bool:
            raise CharacterError('無效的自動施放設定。')
        if target == 'self' and SKILLS[job][slot - 1].effect not in ALLY_EFFECTS | {'stance', 'taunt'}:
            raise CharacterError('攻擊技能不能以自己為目標。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rules = self.rules(guild, user, job)
            old_priority = next(rule.priority for rule in rules if rule.slot == slot)
            updated = []
            for rule in rules:
                if rule.slot == slot:
                    rule = Rule(slot, priority, enabled, condition, target)
                elif rule.priority == priority:
                    rule = Rule(rule.slot, old_priority, rule.enabled, rule.condition, rule.target)
                updated.append((guild, user, job, rule.slot, rule.priority, int(rule.enabled), rule.condition, rule.target))
            self.db.executemany('INSERT OR REPLACE INTO rpg_tactics VALUES (?, ?, ?, ?, ?, ?, ?, ?)', updated)


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
    guard_bonus: int = 0
    stability: tuple = (100, 100)
    armed: bool = True

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
            skill = SKILLS[actor.job][rule.slot - 1]
            if not rule.enabled or self.round < actor.ready.get(rule.slot, 0):
                continue
            if rule.condition == 'self40' and actor.hp * 100 > actor.stats['HP'] * 40:
                continue
            if rule.condition == 'ally50' and not any(f.hp * 2 <= f.stats['HP'] for f in allies):
                continue
            if rule.condition == 'enemies3' and len(enemies) < 3:
                continue
            candidates = allies if skill.effect in ALLY_EFFECTS else enemies
            if skill.effect == 'heal':
                candidates = [f for f in candidates if f.hp < f.stats['HP']]
            elif skill.effect == 'cleanse':
                candidates = [f for f in candidates if f.has('poison', self.round) or f.has('break', self.round)]
            elif skill.effect == 'bless':
                candidates = [f for f in candidates if not f.has(skill.effect, self.round)]
            if skill.effect == 'guard':
                bonus = max(1, actor.stats['HP'] // 20)
                target = actor if any(not f.has('guard', self.round) or f.guard_bonus < bonus for f in allies) else None
            elif skill.effect in ('stance', 'taunt'):
                target = actor if not actor.has(skill.effect, self.round) else None
            else:
                target = self.target(actor, candidates, rule, skill.effect not in ALLY_EFFECTS)
            if target:
                return rule, skill, target
        return None

    def hit(self, actor, target, power=1.0, precise=False):
        if actor.team == 0 and not actor.armed:
            self.log.append(f'{actor.name} 未裝備武器，無法造成傷害。')
            return False
        chance = max(10, min(99, actor.stats['命中率'] - target.stats['閃避率']))
        if not precise and self.rng.random() * 100 >= chance:
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
        target.hp = max(0, target.hp - damage)
        self.log.append(f'{actor.name} → {target.name}：{damage} 傷害{"（暴擊）" if critical else ""}'
                        f'{f"（穩定度 {stability}%）" if actor.stability != (100, 100) else ""}{"，倒下" if target.hp == 0 else ""}')
        return True

    def act(self, actor):
        if actor.team == 1 and actor.job == '鐵殼魔像':
            if actor.has('charged_punch', self.round):
                actor.effects.pop('charged_punch', None)
                target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
                if target is not None:
                    self.log.append(f'{actor.name} 使用【鐵核重拳】')
                    self.hit(actor, target, 2.5)
                return
            if self.round % 3 == 0:
                actor.effects['charged_punch'] = self.round + 1
                self.log.append(f'{actor.name} 使用【蓄力】：下一回合將使出 250% 倍率重拳！')
                return
        if actor.team == 1 and actor.job == '史萊姆群':
            self.log.append(f'{actor.name} 使用【群體彈跳】')
            for _ in range(3):
                target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
                if target is None:
                    break
                self.hit(actor, target, 0.45)
            return
        if actor.team == 1 and actor.job == '巨獸' and self.round % 3 == 0:
            self.log.append(f'{actor.name} 使用【震地橫掃】')
            for enemy in self.living(0):
                self.hit(actor, enemy, 0.75)
            return
        selected = self.select(actor)
        if not selected:
            target = self.target(actor, self.living(1 - actor.team), Rule(0, 0, True, 'always', 'lowest'), True)
            self.log.append(f'{actor.name} 使用普通攻擊')
            hit = self.hit(actor, target)
            if hit and actor.job == '毒蛛' and target.hp > 0:
                target.effects['poison'] = self.round + 2
            return
        rule, skill, target = selected
        actor.ready[rule.slot] = self.round + skill.cooldown + 1
        self.log.append(f'{actor.name} 使用【{skill.name}】')
        effect = skill.effect
        if effect == 'heal':
            healing = actor.stats['治療量'] // 2 if actor.job == '民兵' else actor.stats['治療量']
            amount = min(target.stats['HP'] - target.hp, healing)
            target.hp += amount
            self.log.append(f'{target.name} 恢復 {amount} HP')
        elif effect == 'cleanse':
            target.effects.pop('poison', None)
            target.effects.pop('break', None)
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
        elif effect == 'area':
            for enemy in self.living(1 - actor.team):
                self.hit(actor, enemy, 0.8)
        elif effect == 'double':
            for _ in range(2):
                if target.hp > 0:
                    self.hit(actor, target, 0.85)
        else:
            hit = self.hit(actor, target, 1.6 if effect == 'strike' else 1.5 if effect == 'precise' else 1,
                           precise=effect == 'precise')
            if hit and effect == 'break' and target.hp > 0:
                target.effects['break'] = self.round + 1

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
                damage = max(1, actor.stats['HP'] // 20)
                actor.hp = max(0, actor.hp - damage)
                self.log.append(f'{actor.name} 中毒，損失 {damage} HP')
                if self.check_end():
                    break
                if actor.hp == 0:
                    continue
            self.act(actor)
            if self.check_end():
                break
        if not self.result and self.round >= self.max_rounds:
            self.result = '平手（達回合上限）'



def raid_battle(participants, monster, seed):
    """Only actual registered players fight one generated monster."""
    fighters = [Fighter(p['name'], 0, p['state']['job'], dict(p['state']['combat']),
                        p['state']['total'][3], [Rule(**r) for r in p['rules']],
                        stability=tuple(p['state'].get('stability', (100, 100))),
                        armed=bool(p['state'].get('equipped', {}).get('武器'))) for p in participants]
    average = sum(p['state']['level'] for p in participants) / len(participants)
    stats = {'HP': int(sum(150 + p['state']['level'] * 28 for p in participants)),
             '攻擊': int(22 + average * 6), '防禦': int(10 + average * 2),
             '治療量': 0, '命中率': 92, '閃避率': 5, '暴擊率': 10}
    if monster['kind'] == '鐵殼魔像':
        stats['HP'] = int(stats['HP'] * 1.2)
        stats['防禦'] *= 2
    for stat in ('HP', '攻擊', '防禦'):
        stats[stat] = max(1, int(stats[stat] * monster.get('strength', 1)))
    fighters.append(Fighter(monster['name'], 1, monster['kind'], stats, int(12 + average * 2), []))
    return Battle(fighters, seed=seed)


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
        f.guard_bonus = data_f.get('guard_bonus', 0)
        f.stability = tuple(data_f.get('stability', (100, 100)))
        f.armed = data_f.get('armed', True)
        fighters.append(f)
    battle = Battle(fighters, max_rounds=data['max_rounds'])
    battle.round, battle.result, battle.log = data['round'], data['result'], list(data['log'])
    battle.rng.setstate(tuples(data['random_state']))
    return battle
