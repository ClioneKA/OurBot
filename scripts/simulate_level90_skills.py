"""Prototype Lv.90 skills against the production battle engine; no game data is changed.

Run: python -m scripts.simulate_level90_skills --seeds 100
The prototype uses canonical T90 shop gear and tier-six legendary encounters.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
from statistics import mean
import sys

from core import rpg_battle as rb
from core.rpg_character import CRITICAL_DAMAGE_PERCENT, GROWTH, ITEMS, combat_from_stats
from core.rpg_monsters import prepare_monster


NEW = {
    '裝甲步兵': (
        rb.Skill('戰線重整', 'rally', 5, '', 'self40', rb.PREPARATION_TIMING),
        rb.Skill('浴血回斬', 'strike', 5, ''),
        rb.Skill('裂陣斬', 'cleave', 6, '', 'enemies3'),
    ),
    '騎士': (
        rb.Skill('守望壁壘', 'guard', 6, '', 'ally50', rb.PREPARATION_TIMING,
                 potency_percent=60, guard_immunity=True),
        rb.Skill('變式盾衝', 'shield_bash', 4, ''),
        rb.Skill('制裁衝鋒', 'knight_charge', 6, '', 'ally50'),
    ),
    '弓兵': (
        rb.Skill('雙重毒箭', 'double', 5, ''),
        rb.Skill('疫羽散射', 'area', 6, '', 'enemies3'),
        rb.Skill('催蝕箭', 'poison_arrow', 5, ''),
    ),
    '僧侶': (
        rb.Skill('恩典波紋', 'group_heal', 6, '', 'allies_injured'),
        rb.Skill('合聲聖光', 'holy_light', 5, '', 'ally50'),
        rb.Skill('晨禱祝福', 'bless', 6, '', timing=rb.PREPARATION_TIMING),
    ),
}
OLD_BEGIN = rb.Battle._begin_passive_action
OLD_FINISH = rb.Battle._finish_passive_action
OLD_RESOLVE = rb.Battle._resolve_skill
OLD_HIT = rb.Battle.hit
OLD_DAMAGE = rb.Battle.apply_damage
OLD_STEP = rb.Battle.step
OLD_SELECT = rb.Battle.select


def begin(self, actor, skill=None, target=None, basic=False):
    if skill and skill.name == '變式盾衝':
        last = actor.passive_state.get('lance_last')
        virtual = 'knight_charge' if last == 'shield_bash' else 'shield_bash'
        skill = replace(skill, effect=virtual)
    return OLD_BEGIN(self, actor, skill, target, basic)


def finish(self, context):
    if context['skill'] and context['skill'].name == '合聲聖光':
        context['skill'] = replace(context['skill'], effect='heal')
    result = OLD_FINISH(self, context)
    if (context['skill'] and context['skill'].name == '變式盾衝'
            and context['hits'] and context['actor'].passive_id == 3):
        state = context['actor'].passive_state
        state['lance_stacks'] = min(5, state.get('lance_stacks', 0) + 1)
    return result


def hit(self, actor, target, *args, **kwargs):
    old = actor.stats['攻擊']
    if actor.has('prototype_morning', self.round):
        blessed = actor.bless_attack_percent / 100 if actor.has('bless', self.round) else 0
        factor = (1 + blessed + .2) / (1 + blessed)
        actor.stats['攻擊'] = int(old * factor)
        if kwargs.get('attack_override') is not None:
            kwargs['attack_override'] = int(kwargs['attack_override'] * factor)
    try:
        return OLD_HIT(self, actor, target, *args, **kwargs)
    finally:
        actor.stats['攻擊'] = old


def damage(self, target, amount, share_link=True, direct=False):
    if direct and target.has('prototype_shield', self.round):
        shield = target.status_stacks.get('prototype_shield', 0)
        absorbed = min(shield, max(0, int(amount)))
        target.status_stacks['prototype_shield'] = shield - absorbed
        amount -= absorbed
        if absorbed and target.team == 0:
            self._gain_watch(target)
    return OLD_DAMAGE(self, target, amount, share_link, direct)


def step(self):
    # Delayed healing resolves at the start of the following round.
    for ally in self.living(0):
        pending = ally.status_stacks.pop('prototype_wave', None)
        if pending and pending['round'] == self.round + 1:
            source = next((f for f in self.fighters if f.user_id == pending['source']), None)
            if source:
                self.heal(source, ally, pending['amount'], passive_trigger=False)
    return OLD_STEP(self)


def select(self, actor):
    choice = OLD_SELECT(self, actor)
    if choice and choice[1].name == '催蝕箭':
        source = actor.user_id
        eligible = [enemy for enemy in self.living(1 - actor.team)
                    if any(stack['source_id'] == source
                           for stack in enemy.status_stacks.get('poison_arrows', ()))]
        if eligible:
            return choice[0], choice[1], self.target(actor, eligible, choice[0], True)
        else:
            slot = choice[0].slot
            previous = actor.ready.get(slot)
            actor.ready[slot] = self.round + 1
            try:
                return OLD_SELECT(self, actor)
            finally:
                if previous is None:
                    actor.ready.pop(slot, None)
                else:
                    actor.ready[slot] = previous
    return choice


def poison(self, actor, target, percent):
    if target.hp <= 0 or target.has('immunity', self.round) or target.job == '逆潮法陣':
        return
    attack = actor.stats['攻擊']
    if actor.has('bless', self.round):
        attack *= 1 + actor.bless_attack_percent / 100
    if actor.has('prototype_morning', self.round):
        blessed = actor.bless_attack_percent / 100 if actor.has('bless', self.round) else 0
        attack *= (1 + blessed + .2) / (1 + blessed)
    attack *= self.damage_dealt_multiplier(actor) * self.debuff_damage_multiplier(actor, target)
    target.status_stacks.setdefault('poison_arrows', []).append(dict(
        source_id=actor.user_id, damage=max(1, int(attack * percent / 100)),
        next_round=self.round + 1, remaining=2))


def instant_poison(self, actor, target):
    pending = [s for s in target.status_stacks.get('poison_arrows', ())
               if s['source_id'] == actor.user_id and s['remaining'] > 0]
    if not pending:
        return
    stack = min(pending, key=lambda s: s['next_round'])
    damage = self.toxic_damage(actor, target, stack['damage'])
    actual, _, partner = self.apply_damage(target, damage)
    dealt = actual + partner
    actor.combat_stats['damage_dealt'] += dealt
    actor.combat_stats['direct_damage'] += dealt
    if actor.passive_id == 2 and dealt:
        toxicity = target.status_stacks.setdefault('passive_toxicity', {})
        key = str(actor.user_id)
        toxicity[key] = min(5, toxicity.get(key, 0) + 1)
    stack['remaining'] -= 1
    stack['next_round'] += 1
    if not stack['remaining']:
        target.status_stacks['poison_arrows'].remove(stack)


def resolve(self, actor, rule, skill, target):
    name = skill.name
    if name not in {s.name for skills in NEW.values() for s in skills}:
        return OLD_RESOLVE(self, actor, rule, skill, target)
    if name == '守望壁壘':
        OLD_RESOLVE(self, actor, rule, skill, target)
        for ally in self.living(actor.team):
            ally.effects['prototype_shield'] = self.round + 1
            ally.status_stacks['prototype_shield'] = max(
                ally.status_stacks.get('prototype_shield', 0), actor.stats['HP'] * 8 // 100)
        return
    self.record_skill(actor, name)
    actor.ready[rule.slot] = self.round + max(2, skill.cooldown - actor.cooldown_reduction)
    self.log.append(f'{actor.name} 使用【{name}】')
    if name == '戰線重整':
        self.clear_negative_effects(actor, 1)
        self.heal(actor, actor, actor.stats['HP'] * 8 // 100)
    elif name == '浴血回斬':
        if self.hit(actor, target, 2.1) and actor.hp * 100 <= actor.stats['HP'] * 60:
            self.heal(actor, actor, actor.stats['HP'] * 8 // 100)
    elif name == '裂陣斬':
        for enemy in self.living(1 - actor.team):
            if self.hit(actor, enemy, 1.3, attack_scope='group') and enemy is target and enemy.hp > 0:
                if self.apply_debuff(enemy, 'break', self.round + 1, actor):
                    enemy.status_stacks['break_defense_percent'] = 80
                    enemy.status_stacks['break_defense_until'] = enemy.effects.get('break')
    elif name == '變式盾衝':
        if self.hit(actor, target, .5, attack_override=actor.stats['HP']):
            if target.job == '星蝕巨神' and self.mechanics.get('star_charging'):
                self.mechanics['star_charging'] = False
                target.effects['break'] = self.round + 1
    elif name == '制裁衝鋒':
        if self.hit(actor, target, .6, attack_override=actor.stats['HP']):
            injured = [f for f in self.living(actor.team) if f.hp < f.stats['HP']]
            if injured:
                ally = min(injured, key=lambda f: f.hp / f.stats['HP'])
                self.heal(actor, ally, actor.stats['HP'] * 8 // 100)
    elif name == '雙重毒箭':
        for _ in range(2):
            if target.hp <= 0:
                break
            if self.hit(actor, target, .75):
                poison(self, actor, target, 45)
    elif name == '疫羽散射':
        for enemy in self.living(1 - actor.team):
            landed = False
            for _ in range(3):
                if enemy.hp > 0:
                    landed = self.hit(actor, enemy, .3, attack_scope='group') or landed
            if landed:
                poison(self, actor, enemy, 35)
    elif name == '催蝕箭':
        if self.hit(actor, target, 2):
            instant_poison(self, actor, target)
    elif name == '恩典波紋':
        for ally in self.living(actor.team):
            self.heal(actor, ally, actor.stats['治療量'] * 50 // 100)
            ally.status_stacks['prototype_wave'] = dict(
                round=self.round + 1, source=actor.user_id,
                amount=actor.stats['治療量'] * 10 // 100)
    elif name == '合聲聖光':
        for enemy in self.living(1 - actor.team):
            self.hit(actor, enemy, .9, attack_scope='group')
        injured = [f for f in self.living(actor.team) if f.hp < f.stats['HP']]
        if injured:
            ally = min(injured, key=lambda f: f.hp / f.stats['HP'])
            self.heal(actor, ally, actor.stats['治療量'] * 70 // 100)
    elif name == '晨禱祝福':
        for ally in self.living(actor.team):
            ally.effects['prototype_morning'] = self.round + 1
        if self._passive_action is not None:
            self._passive_action['hymn_bless'] = True


BASE = {
    '裝甲步兵': {1: (2, 5, 4), 2: (3, 2, 5), 3: (3, 5, 1)},
    '騎士': {1: (1, 2, 3), 2: (2, 4, 3), 3: (2, 4, 3)},
    '弓兵': {1: (2, 4, 1), 2: (2, 5, 4), 3: (2, 4, 1)},
    '僧侶': {1: (4, 1, 2), 2: (1, 2, 5), 3: (4, 1, 5)},
}
CASES = (
    ('裝甲步兵', 2, '戰線重整', (6, 2, 5)),
    ('裝甲步兵', 3, '浴血回斬', (3, 7, 1)),
    ('裝甲步兵', 1, '裂陣斬', (2, 5, 8)),
    ('騎士', 2, '守望壁壘', (6, 4, 3)),
    ('騎士', 3, '變式盾衝', (2, 4, 7)),
    ('騎士', 1, '制裁衝鋒', (1, 2, 8)),
    ('弓兵', 2, '雙重毒箭', (2, 6, 4)),
    ('弓兵', 2, '疫羽散射', (2, 7, 4)),
    ('弓兵', 2, '催蝕箭', (2, 5, 8)),
    ('僧侶', 1, '恩典波紋', (6, 1, 2)),
    ('僧侶', 2, '合聲聖光', (1, 2, 7)),
    ('僧侶', 2, '晨禱祝福', (1, 8, 5)),
    ('僧侶', 2, '合聲＋晨禱', (7, 8, 1)),
)


def participant(job, uid, skills):
    total = tuple(28 + 86 * weight for weight in GROWTH[job])
    equipped = {'武器': f'{job}:3:武器', '套裝': f'{job}:3:套裝'}
    weapon, suit = (ITEMS[equipped[key]] for key in ('武器', '套裝'))
    combat = combat_from_stats(total, job)
    for i, stat in enumerate(('HP', '攻擊', '防禦', '治療量')):
        combat[stat] += weapon.combat[i] + suit.combat[i]
    combat['命中率'] += weapon.accuracy
    combat['閃避率'] += suit.evasion
    rules = []
    for slot, skill_id in enumerate(skills, 1):
        skill = rb.SKILLS[job][skill_id - 1]
        condition, threshold, target = skill.condition, None, rb.default_target(skill)
        if skill.name == '戰線重整':
            threshold = 60
        elif skill.name in ('守望壁壘', '制裁衝鋒'):
            threshold = 70
        elif skill.name in ('裂陣斬', '疫羽散射'):
            threshold = 2
        elif skill.name == '恩典波紋':
            threshold = 2
        elif skill.name == '雙重毒箭':
            target = 'lowest'
        elif skill.name == '合聲聖光':
            threshold = 80
        elif skill.name == '晨禱祝福':
            target = 'strongest'
        elif skill.effect == 'bless':
            target = 'strongest'
        elif skill.effect == 'cleanse':
            target = 'debuffed'
        elif skill.effect == 'hindering_shot':
            condition, target = 'enemy_charging', 'boss'
        elif job == '弓兵' and skill.name == '箭雨':
            threshold = 2
        elif skill.effect in ('heal', 'group_heal', 'holy_light'):
            threshold = 80
        rules.append(asdict(rb.Rule(slot, slot, True, condition, target, skill_id, threshold)))
    return dict(id=uid, name=f'{job}{uid}', rules=rules, state=dict(
        level=90, job=job, total=total, combat=combat, equipped=equipped,
        stability=weapon.stability, damage_guard_chance=suit.damage_guard_chance,
        vulnerable_chance=weapon.vulnerable_chance,
        vulnerable_percent=weapon.vulnerable_percent,
        critical_damage_percent=CRITICAL_DAMAGE_PERCENT[job]))


def fight(monster, party, seed, job, passive_id, skills):
    members = [participant(j, i + 1, skills if j == job and i == party.index(job) else BASE[j][1])
               for i, j in enumerate(party)]
    index = party.index(job)
    members[index]['passive_id'] = passive_id
    battle = rb.raid_battle(members, monster, seed)
    while not battle.result and battle.round < 80:
        battle.step()
    actor = battle.fighters[index]
    allies = battle.fighters[:len(party)]
    return dict(win=int(battle.result == '勝利'), rounds=battle.round,
                actor_dpr=actor.combat_stats['damage_dealt'] / max(1, battle.round),
                team_dpr=sum(f.combat_stats['damage_dealt'] for f in allies) / max(1, battle.round),
                heal=actor.combat_stats['healing_done'], alive=int(actor.hp > 0),
                team_alive=sum(f.hp > 0 for f in allies),
                uses=actor.combat_stats['skills_used'].get(
                    next((s.name for s in NEW[job] if s.name in [rb.SKILLS[job][i - 1].name for i in skills]), ''), 0),
                hymn=actor.passive_state.get('hymn_procs', 0),
                lance=actor.passive_state.get('lance_stacks', 0))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    for job, skills in NEW.items():
        # Keep the historical prototype reproducible after the live skills ship.
        rb.SKILLS[job] = rb.SKILLS[job][:5] + skills
    rb.Battle._begin_passive_action = begin
    rb.Battle._finish_passive_action = finish
    rb.Battle._resolve_skill = resolve
    rb.Battle.hit = hit
    rb.Battle.apply_damage = damage
    rb.Battle.step = step
    rb.Battle.select = select
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=100)
    parser.add_argument('--only', help='Run one named proposed skill or loadout.')
    parser.add_argument('--passive-off', action='store_true',
                        help='Ablate the tested fighter passive for comparison.')
    parser.add_argument('--output', default='local_designs/rpg-level90-skills-simulation.json')
    args = parser.parse_args()
    rows = []
    for party in (list(BASE), list(BASE) + ['裝甲步兵', '弓兵']):
        for kind in ('星蝕巨神', '逆潮聖骸'):
            monster = prepare_monster(dict(kind=kind, name=kind), quality='傳說')
            for job, passive, skill, proposal in CASES:
                if args.only and skill != args.only:
                    continue
                baseline = (2, 3, 4) if skill == '疫羽散射' else BASE[job][passive]
                equipped_passive = None if args.passive_off else passive
                old = [fight(monster, party, seed, job, equipped_passive, baseline)
                       for seed in range(args.seeds)]
                new = [fight(monster, party, seed, job, equipped_passive, proposal)
                       for seed in range(args.seeds)]
                average = lambda samples: {key: mean(item[key] for item in samples) for key in samples[0]}
                rows.append(dict(size=len(party), monster=kind, job=job, passive=passive,
                                 skill=skill, baseline_skills=baseline, proposed_skills=proposal,
                                 baseline=average(old), proposed=average(new)))
            print(f'Completed {len(party)} players / {kind}', flush=True)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(seeds=args.seeds, passive_off=args.passive_off, rows=rows),
                               ensure_ascii=False, indent=2),
                    encoding='utf-8')
    for job, passive, skill, _ in CASES:
        if args.only and skill != args.only:
            continue
        group = [r for r in rows if r['job'] == job and r['passive'] == passive and r['skill'] == skill]
        print(f'{job} {skill}: win {mean(r["proposed"]["win"]-r["baseline"]["win"] for r in group)*100:+.1f} pp, '
              f'actor DPR {mean((r["proposed"]["actor_dpr"]/r["baseline"]["actor_dpr"]-1)*100 for r in group):+.1f}%, '
              f'heal {mean(r["proposed"]["heal"]-r["baseline"]["heal"] for r in group):+.0f}', flush=True)


if __name__ == '__main__':
    main()
