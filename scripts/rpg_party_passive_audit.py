"""Paired passive ablations using the current production raid engine (no DB access).

Run from the repository root: python -m scripts.rpg_party_passive_audit --seeds 300
Each passive gets a compatible three-skill loadout, held identical in its control.
Other party members have no passive. This measures marginal benefit, not build rank.
Generated reports default to the Git-ignored local_designs/rpg-passive-balance directory.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from statistics import mean, stdev

from core.rpg_battle import PASSIVES, Rule, raid_battle
from core.rpg_character import CRITICAL_DAMAGE_PERCENT, GROWTH, ITEMS, combat_from_stats
from core.rpg_monsters import prepare_monster


JOBS = tuple(PASSIVES)
DEFAULTS = {JOBS[0]: (2, 5, 1), JOBS[1]: (1, 2, 3),
            JOBS[2]: (2, 4, 1), JOBS[3]: (4, 1, 2)}
LOADOUTS = {
    JOBS[0]: {1: (2, 5, 1), 2: (3, 2, 5), 3: (3, 5, 1)},
    JOBS[1]: {1: (1, 2, 3), 2: (1, 2, 3), 3: (1, 4, 3)},
    JOBS[2]: {1: (2, 4, 1), 2: (2, 5, 4), 3: (2, 4, 1)},
    JOBS[3]: {1: (4, 1, 2), 2: (3, 1, 2), 3: (4, 1, 5)},
}


def participant(job, uid, skills):
    growth = GROWTH[job]
    total = tuple(28 + 44 * weight for weight in growth)  # Lv.50, promotion stage 2
    equipped = {'武器': f'{job}:2:武器', '套裝': f'{job}:2:套裝'}
    weapon, suit = (ITEMS[equipped[key]] for key in ('武器', '套裝'))
    combat = combat_from_stats(total, job)
    for i, stat in enumerate(('HP', '攻擊', '防禦', '治療量')):
        combat[stat] += weapon.combat[i] + suit.combat[i]
    combat['命中率'] += weapon.accuracy
    combat['閃避率'] += suit.evasion
    rules = []
    for slot, skill in enumerate(skills, 1):
        condition, target, threshold = 'always', 'lowest', None
        if job == '弓兵' and skill == 2:
            condition, target = 'enemy_charging', 'boss'
        if job == '僧侶':
            if skill == 3:
                condition, target = 'ally_debuff', 'debuffed'
            elif skill == 2:
                target = 'strongest'
            elif skill == 4:
                condition, threshold = 'allies_injured', 2
            elif skill == 1:
                condition, threshold = 'ally50', 80
        rules.append(asdict(Rule(slot, slot, True, condition, target, skill, threshold)))
    return dict(id=uid, name=f'{job}{uid}', rules=rules,
                state=dict(level=50, job=job, total=total, combat=combat,
                           equipped=equipped, stability=weapon.stability,
                           damage_guard_chance=suit.damage_guard_chance,
                           vulnerable_chance=weapon.vulnerable_chance,
                           vulnerable_percent=weapon.vulnerable_percent,
                           critical_damage_percent=CRITICAL_DAMAGE_PERCENT[job]))


def run(party, monster, seed, index, passive_id):
    party[index]['passive_id'] = passive_id
    battle = raid_battle(party, monster, seed)
    while not battle.result:
        battle.step()
    allies = battle.fighters[:len(party)]
    actor = allies[index]
    stats = actor.combat_stats
    blood_procs = sum('的浴血戰意消耗' in line for line in battle.log)
    return dict(win=float(battle.result == '勝利'), rounds=battle.round,
                damage=stats['damage_dealt'], healing=stats['healing_done'],
                actor_survived=float(actor.hp > 0),
                healing_received=stats['healing_received'],
                blood_hp_paid=blood_procs * max(1, actor.stats['HP'] * 5 // 100),
                team_dpr=sum(f.combat_stats['damage_dealt'] for f in allies) / battle.round,
                actor_dpr=stats['damage_dealt'] / battle.round,
                team_healing=sum(f.combat_stats['healing_done'] for f in allies),
                survivors=sum(f.hp > 0 for f in allies),
                hymn_procs=actor.passive_state.get('hymn_procs', 0),
                grace_procs=sum('的【恩典回響】額外' in line for line in battle.log),
                blood_procs=blood_procs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=300)
    parser.add_argument('--output', default='local_designs/rpg-passive-balance/rpg-party-passive-latest.json')
    parser.add_argument('--no-guard', action='store_true', help='Sensitivity: knight taunt/charge/bash')
    parser.add_argument('--passive-names', nargs='+', help='Only these passive names')
    parser.add_argument('--guard-without-taunt', action='store_true')
    args = parser.parse_args()
    if args.seeds < 2:
        parser.error('at least two seeds required')
    rows = []
    for size in (4, 6):
        for kind in ('熔爐鎧獸', '迷霧菌后'):
            monster = prepare_monster(dict(kind=kind, name=kind), quality='普通')
            composition = list(JOBS) + ([JOBS[0], JOBS[2]] if size == 6 else [])
            for job in JOBS:
                index = composition.index(job)
                for passive in PASSIVES[job]:
                    if args.passive_names and passive.name not in args.passive_names:
                        continue
                    party = [participant(j, i + 1, (1, 3, 4) if args.no_guard and j == '騎士'
                                         else DEFAULTS[j]) for i, j in enumerate(composition)]
                    skills = ((2, 4, 3) if args.guard_without_taunt and job == '騎士' and passive.id in (2, 3)
                              else LOADOUTS[job][passive.id])
                    party[index] = participant(job, index + 1, skills)
                    base, enabled = [], []
                    for seed in range(args.seeds):
                        base.append(run(party, monster, seed, index, None))
                        enabled.append(run(party, monster, seed, index, passive.id))
                    b = {k: mean(r[k] for r in base) for k in base[0]}
                    p = {k: mean(r[k] for r in enabled) for k in enabled[0]}
                    deltas = [a['win'] - c['win'] for a, c in zip(enabled, base)]
                    row = dict(size=size, kind=kind, job=job, passive=passive.name,
                               skills=skills, baseline=b, enabled=p,
                               win_delta_pp=(p['win']-b['win'])*100,
                               win_delta_ci95_pp=1.96*stdev(deltas)/(args.seeds**.5)*100,
                               team_dpr_pct=(p['team_dpr']/b['team_dpr']-1)*100,
                               actor_dpr_pct=(p['actor_dpr']/b['actor_dpr']-1)*100)
                    rows.append(row)
            print(f'Completed {size} players / {kind}', flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(seeds=args.seeds, no_guard=args.no_guard,
                                                guard_without_taunt=args.guard_without_taunt,
                                                passive_descriptions={p.name: p.description for values in PASSIVES.values() for p in values},
                                                rows=rows), ensure_ascii=False, indent=2), encoding='utf-8')
    for job in JOBS:
        for passive in PASSIVES[job]:
            group = [r for r in rows if r['job'] == job and r['passive'] == passive.name]
            if not group:
                continue
            print(passive.name, 'win pp', round(mean(r['win_delta_pp'] for r in group), 2),
                  'team DPR%', round(mean(r['team_dpr_pct'] for r in group), 2),
                  'actor DPR%', round(mean(r['actor_dpr_pct'] for r in group), 2),
                  'heal delta', round(mean(r['enabled']['healing']-r['baseline']['healing'] for r in group), 1), flush=True)


if __name__ == '__main__':
    main()
