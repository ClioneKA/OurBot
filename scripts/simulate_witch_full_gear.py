"""Reproducible full-slot witch calibration using real character snapshots.

Run with python -m scripts.simulate_witch_full_gear --help. This writes reports
only; production balance and player databases are never modified.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from itertools import combinations
import json
from pathlib import Path
import random
from statistics import mean
import tempfile

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Rule
from core.rpg_character import Characters, ITEMS, item_level
from core.rpg_witch_battle import witch_battle_from_participants
from core.rpg_witch_catalog import IDS, PROFILE
from core.settings import RPGSettings
from scripts.rpg_balance import T30_EQUIPMENT, T40_EQUIPMENT, T45_EQUIPMENT
from scripts.simulate_tier56 import SETS
from scripts.witch_mechanic_policy import MECHANIC_SKILLS, choose_round

JOBS = ('裝甲步兵', '騎士', '弓兵', '僧侶')
JOB_KEYS = dict(zip(JOBS, ('infantry', 'knight', 'archer', 'monk')))
SKILL_IDS = {'裝甲步兵': (5, 1, 2), '騎士': (2, 1, 3),
             '弓兵': (4, 1, 2), '僧侶': (4, 1, 5)}
ACCESSORIES = {
    '裝甲步兵': ('twin_beast:charm', 'whale:charm', 'cycle:emblem', 'raid:1', 'raid:0', 'raid:2'),
    '騎士': ('goblin:badge', 'whale:charm', 'cycle:emblem', 'raid:0', 'raid:2', 'raid:1'),
    '弓兵': ('twin_beast:charm', 'cycle:emblem', 'raid:3', 'raid:1', 'raid:0', 'raid:2'),
    '僧侶': ('puppet:twin_charm', 'cycle:emblem', 'raid:4', 'raid:0', 'raid:2', 'raid:1'),
}
COMBOS = list(combinations(IDS, 3))


def equipment(job, level):
    if level >= 70:
        return tuple(f'maze:{JOB_KEYS[job]}:{slot}' for slot in ('weapon', 'suit'))
    if level >= 60:
        return SETS[6][job]
    if level >= 50:
        return SETS[5][job]
    if level >= 45:
        return tuple(key + ':red' for key in T45_EQUIPMENT[job])
    if level >= 40:
        return T40_EQUIPMENT[job]
    if level >= 30:
        return T30_EQUIPMENT[job]
    if level >= 20:
        weapon = {'裝甲步兵': 'goblin:axe', '騎士': 'bat:sword_shield',
                  '弓兵': 'goblin:bow', '僧侶': 'golem:staff'}[job]
        return weapon, f'tree:{JOB_KEYS[job]}'
    return f'{job}:0:武器', f'{job}:0:套裝'


def party(levels, jobs=JOBS, strategy='mechanics'):
    """Temporary DB ensures real equip restrictions, set bonuses and passives."""
    with tempfile.TemporaryDirectory() as directory:
        store = RPGStore(Path(directory) / 'rpg.db')
        try:
            settings = RPGSettings()
            chars = Characters(store, settings)
            participants = []
            for uid, (job, level) in enumerate(zip(jobs, levels), 1):
                if level < 10:
                    raise ValueError('The four-profession reference party requires level >= 10.')
                store.award_voice([(1, uid, level_floor(level))])
                chars.change_job(1, uid, job)
                for key in equipment(job, level):
                    instance = chars.grant_item(1, uid, key)[0]
                    chars.equip(1, uid, instance)
                capacity = chars.snapshot(1, uid)['capacity']
                candidates = [key for key in ACCESSORIES[job] if item_level(ITEMS[key], settings) <= level]
                assert len(candidates) >= capacity
                for slot, key in enumerate(candidates[:capacity], 1):
                    instance = chars.grant_item(1, uid, key)[0]
                    chars.equip(1, uid, instance, slot)
                state = chars.snapshot(1, uid)
                assert len(state['equipped']) == capacity + 2
                loadouts = MECHANIC_SKILLS if strategy == 'mechanics' else SKILL_IDS
                skills = loadouts[job] if level >= 20 else (1, 2, 3)
                participants.append(dict(id=uid, name=f'{job}{uid}', state=state,
                    passive_id=(2 if job == '騎士' else 1) if level >= 50 else None,
                    rules=[asdict(Rule(slot, slot, True, 'always', 'lowest', skill_id=skill))
                           for slot, skill in enumerate(skills, 1)]))
            return participants
        finally:
            store.close()


def power(participants):
    b = witch_battle_from_participants(participants, IDS[:3], seed=0)
    players = b.living(0)
    # A transparent initial guess only; empirical calibration follows.
    offense = sum(p.stats['攻擊'] * (1 + p.stats['暴擊率'] / 100
                  * (p.critical_damage_percent / 100 - 1)) for p in players)
    return dict(hp=mean(p.stats['HP'] for p in players), offense=offense / len(players))


def simulate(participants, ids, seed, scales=None, strategy='mechanics'):
    b = witch_battle_from_participants(participants, ids, seed=seed)
    if scales is not None:
        for witch in b.witches():
            profile = PROFILE[witch.job]
            for stat, value, scale in zip(('HP', '攻擊', '防禦'), profile[2:5], scales):
                witch.stats[stat] = max(1, round(value * scale *
                    ((.4 + .15 * len(participants)) if stat == 'HP' else 1)))
            witch.hp = witch.stats['HP']
    decisions = Counter()
    if strategy == 'auto':
        b.auto_players = b.living_player_ids()
    while not b.result:
        if strategy == 'mechanics':
            choose_round(b, decisions)
            assert b.ready_to_resolve()
        b.resolve(use_defaults=strategy == 'auto')
    return dict(win=b.result == '勝利', rounds=b.round,
                timeout='回合上限' in b.result,
                survivors=len(b.living(0)), decisions=dict(decisions), events=dict(b.events))


def evaluate(participants, cases, scales=None, strategy='mechanics'):
    rows = [dict(ids=ids, seed=seed, **simulate(participants, ids, seed, scales, strategy))
            for ids, seed in cases]
    wins = [row for row in rows if row['win']]
    return dict(n=len(rows), wins=len(wins), win_rate=len(wins) / len(rows),
                rounds=mean(row['rounds'] for row in rows),
                victory_rounds=mean(row['rounds'] for row in wins) if wins else None,
                timeout_rate=mean(row['timeout'] for row in rows), rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--levels', type=int, nargs='+', default=[10, 20, 30, 40, 45, 50, 60, 90, 120])
    parser.add_argument('--mode', choices=['baseline', 'fit', 'validate', 'checks'], default='baseline')
    parser.add_argument('--samples', type=int, default=64, help='Number of tuning combinations (seed 0).')
    parser.add_argument('--tuning-seeds', type=int, default=3)
    parser.add_argument('--seeds', type=int, default=3, help='Independent validation seeds per combination.')
    parser.add_argument('--seed-start', type=int, default=1000)
    parser.add_argument('--hp-factor', type=float, default=1.0)
    parser.add_argument('--output', type=Path, default=Path('docs/witch-full-gear-balance.json'))
    parser.add_argument('--fit-input', type=Path)
    parser.add_argument('--strategy', choices=['mechanics', 'auto'], default='mechanics')
    args = parser.parse_args()
    if args.seeds < 1 or args.samples < 1 or args.tuning_seeds < 1 or args.hp_factor <= 0:
        parser.error('Seeds, samples and HP factor must be positive.')
    if args.mode in ('validate', 'checks') and not args.fit_input:
        parser.error('--fit-input is required for validation and checks.')
    rng = random.Random(7281)
    tuning = rng.sample(COMBOS, min(args.samples, len(COMBOS)))
    cases = [(ids, seed) for ids in tuning for seed in range(args.tuning_seeds)] if args.mode == 'fit' else [
        (ids, seed) for ids in COMBOS for seed in range(args.seed_start, args.seed_start + args.seeds)]
    reference = power(party([50] * 4, strategy=args.strategy))
    fitted = json.loads(args.fit_input.read_text(encoding='utf-8')) if args.fit_input else None
    report = dict(mode=args.mode, target_win_rate=.5, reference=reference,
                  policy=args.strategy,
                  extras='no crystals, embroideries, food, potions, tavern meals or fortune',
                  levels={})
    if args.mode == 'checks':
        scenarios = {
            'three_level50': ([50] * 3, ('騎士', '弓兵', '僧侶')),
            'six_level50': ([50] * 6, ('裝甲步兵', '裝甲步兵', '騎士', '弓兵', '弓兵', '僧侶')),
            'mixed_mean50': ([20, 40, 60, 80], JOBS),
            'mixed_mean50_low_healer': ([80, 60, 40, 20], JOBS),
            'four_level19': ([19] * 4, JOBS),
            'four_level49': ([49] * 4, JOBS),
        }
        knots = sorted(map(int, fitted['levels']))
        report['scenarios'] = {}
        for name, (levels, jobs) in scenarios.items():
            average = mean(levels)
            lower = max(k for k in knots if k <= average)
            upper = min(k for k in knots if k >= average)
            a, z = (fitted['levels'][str(k)]['scales'] for k in (lower, upper))
            fraction = (average - lower) / (upper - lower) if upper != lower else 0
            scales = [x + (y - x) * fraction for x, y in zip(a, z)]
            result = evaluate(party(levels, jobs, args.strategy), cases, scales, args.strategy)
            report['scenarios'][name] = dict(levels=levels, jobs=jobs, scales=scales, **result)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(dict(scenario=name,
                **{k: v for k, v in result.items() if k != 'rows'}), ensure_ascii=False), flush=True)
        return
    for level in args.levels:
        participants = party([level] * 4, strategy=args.strategy)
        metrics = power(participants)
        scales = None
        trials = []
        if args.mode == 'fit':
            hp = args.hp_factor * metrics['offense'] / reference['offense']
            defense = metrics['offense'] / reference['offense']
            # Sweep and bracket before bisection: difficulty includes timeouts.
            sweep = (0.02, .1, .2, .35, .5, .75, 1.0, 1.5, 2.0)
            if fitted:
                previous = fitted['levels'][str(level)]['scales'][1] * reference['hp'] / metrics['hp']
                sweep = (previous * .75, previous * 1.25, previous * 1.75)
            for attack in sweep:
                candidate = (hp, attack * metrics['hp'] / reference['hp'], defense)
                result = evaluate(participants, cases, candidate, args.strategy)
                trials.append(dict(scales=candidate, win_rate=result['win_rate']))
                if result['win_rate'] <= .5:
                    break
            if trials[0]['win_rate'] < .5:
                raise ValueError(f'Lv.{level}: HP causes <50% wins even with minimal attack; reduce --hp-factor.')
            if trials[-1]['win_rate'] > .5:
                raise ValueError(f'Lv.{level}: attack sweep did not bracket the target win rate.')
            low = trials[-2]['scales'][1] if len(trials) > 1 else 0
            high = trials[-1]['scales'][1]
            for _ in range(6):
                candidate = (hp, (low + high) / 2, defense)
                result = evaluate(participants, cases, candidate, args.strategy)
                trials.append(dict(scales=candidate, win_rate=result['win_rate']))
                if result['win_rate'] > .5:
                    low = candidate[1]
                else:
                    high = candidate[1]
            scales = min(trials, key=lambda r: abs(r['win_rate'] - .5))['scales']
        elif args.mode == 'validate':
            scales = fitted['levels'][str(level)]['scales']
        result = evaluate(participants, cases, scales, args.strategy)
        report['levels'][str(level)] = dict(scales=scales, power=metrics,
            participants=participants, trials=trials, **result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(level=level, scales=scales,
            **{k: v for k, v in result.items() if k != 'rows'}), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
