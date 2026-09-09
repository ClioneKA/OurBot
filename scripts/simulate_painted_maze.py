"""Deterministic end-to-end balance matrix for the Painted Maze.

The simulator uses real character snapshots and the equipment players are
expected to own at entry.  It intentionally runs the entire three-painting
route with carried HP instead of treating each boss as a fresh raid.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import statistics
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Rule
from core.rpg_character import Characters
from core.rpg_painted_maze import COLOR_CONTRACTS, PaintedMazeStore, draw_painting_route
from core.rpg_painted_maze_battle import simulate_final_battle, simulate_painting
from core.settings import RPGSettings


JOBS = ('裝甲步兵', '騎士', '弓兵', '僧侶')
T50_EQUIPMENT = {
    '裝甲步兵': ('forge:infantry:weapon', 'forge:infantry:suit'),
    '騎士': ('forge:knight:weapon', 'forge:knight:suit'),
    '弓兵': ('fungus:archer:weapon', 'fungus:archer:suit'),
    '僧侶': ('fungus:monk:weapon', 'fungus:monk:suit'),
}
T60_MAZE_EQUIPMENT = {
    job: (f'maze:{slug}:weapon', f'maze:{slug}:suit')
    for job, slug in zip(JOBS, ('infantry', 'knight', 'archer', 'monk'))
}
T60_RAID_EQUIPMENT = {
    '裝甲步兵': ('star:infantry:weapon', 'star:infantry:suit'),
    '騎士': ('tide:knight:weapon', 'tide:knight:suit'),
    '弓兵': ('star:archer:weapon', 'star:archer:suit'),
    '僧侶': ('tide:monk:weapon', 'tide:monk:suit'),
}
# Four distinct, obtainable accessories (Lv.60 capacity); no maze rewards.
FULL_ACCESSORIES = {
    '裝甲步兵': ('cycle:emblem', 'goblin:badge', 'puppet:twin_charm', 'whale:charm'),
    '騎士': ('cycle:emblem', 'goblin:badge', 'puppet:twin_charm', 'whale:charm'),
    '弓兵': ('cycle:emblem', 'goblin:badge', 'puppet:twin_charm', 'twin_beast:charm'),
    '僧侶': ('cycle:emblem', 'goblin:badge', 'puppet:twin_charm', 'whale:charm'),
}
ACCESSORY_EMBROIDERY = {'裝甲步兵': 'flame', '騎士': 'shield', '弓兵': 'wing', '僧侶': 'star'}
BALANCED_PRIORITY = (
    'crimson', 'azure', 'gold', 'verdant:shelter', 'crimson:edge', 'azure:vitality',
    'gold:aim', 'crimson:precision', 'verdant', 'violet:insight', 'azure:armor',
    'violet:focus', 'verdant:renewal', 'black', 'gold:evasion', 'violet', 'black:gamble', 'black:ruin',
)
CONTRACT_PATTERNS = {
    '緋紅三疊': ('crimson',) * 3,
    '蒼藍三疊': ('azure',) * 3,
    '金黃三疊': ('gold',) * 3,
    '翠綠三疊': ('verdant',) * 3,
    '紫蝕三疊': ('violet',) * 3,
    '漆黑三疊': ('black',) * 3,
    '攻守雙色': ('crimson', 'azure', 'crimson'),
    '續航雙色': ('verdant', 'azure', 'verdant'),
    '速攻雙色': ('gold', 'black', 'gold'),
    '三原色': ('crimson', 'azure', 'gold'),
}


def rule(slot, priority, condition, target, skill_id, value=None):
    return Rule(slot, priority, True, condition, target, skill_id, value)


def maze_rules(job):
    """One legal loadout that reacts to charging, adds and cleanse checks."""
    if job == '騎士':
        return [rule(1, 1, 'enemy_charging', 'boss', 4),
                rule(2, 2, 'ally50', 'lowest', 2, 50),
                rule(3, 3, 'always', 'self', 1)]
    if job == '僧侶':
        return [rule(1, 1, 'ally_debuff', 'debuffed', 3),
                rule(2, 2, 'ally50', 'lowest', 4, 65),
                rule(3, 3, 'ally50', 'lowest', 1, 80)]
    if job == '裝甲步兵':
        return [rule(1, 1, 'mechanic_target', 'mechanic', 5),
                rule(2, 2, 'enemy_broken', 'boss', 2),
                rule(3, 3, 'always', 'lowest', 1)]
    return [rule(1, 1, 'mechanic_target', 'mechanic', 4),
            rule(2, 2, 'enemies3', 'add', 3, 3),
            rule(3, 3, 'always', 'lowest', 1)]


def build_participants(level, count, gear='raid', *, accessories=False):
    directory = tempfile.TemporaryDirectory()
    store = RPGStore(Path(directory.name) / 'maze-balance.db')
    characters = Characters(store, RPGSettings())
    equipment = (T50_EQUIPMENT if level < 60 else
                 T60_MAZE_EQUIPMENT if gear == 'maze' else T60_RAID_EQUIPMENT)
    participants = []
    try:
        for index in range(count):
            user_id, job = index + 1, JOBS[index % len(JOBS)]
            store.award_voice([(1, user_id, level_floor(level))])
            characters.change_job(1, user_id, job)
            for item_id in equipment[job]:
                characters.grant_item(1, user_id, item_id)
                characters.equip(1, user_id, item_id)
            if accessories:
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO rpg_wallets VALUES (?,?,?)', (1, user_id, 10000))
                for slot, item_id in enumerate(FULL_ACCESSORIES[job], 1):
                    instance_id = characters.grant_item(1, user_id, item_id)[0]
                    token = f'instance:{instance_id}'
                    characters.embroider_accessory(1, user_id, token, ACCESSORY_EMBROIDERY[job])
                    characters.equip(1, user_id, token, slot)
            participants.append({
                'id': user_id, 'name': f'{job}{index + 1}',
                'state': characters.snapshot(1, user_id),
                'rules': [asdict(item) for item in maze_rules(job)],
                'passive_id': 1,
            })
    finally:
        store.close()
        directory.cleanup()
    return participants


def offered_contract(seed, checkpoint, chosen):
    """Prefer a new colour and then a balanced benefit; never see future offers."""
    colors = Counter(COLOR_CONTRACTS[key]['color'] for key in chosen)
    candidates = PaintedMazeStore._contract_candidates({'seed': seed}, checkpoint)
    return min(candidates, key=lambda key: (colors[COLOR_CONTRACTS[key]['color']], BALANCED_PRIORITY.index(key)))


def independent_benchmark(seeds=200, *, counts=(8,), level=60, accessories=True, seed_start=0):
    """Every encounter starts at full HP, regardless of earlier victories."""
    rows = []
    for count in counts:
        participants = build_participants(level, count, accessories=accessories)
        results = {key: Counter() for key in ('stage1', 'stage2', 'stage3', 'shadow', 'noah')}
        for seed in range(seed_start, seed_start + seeds):
            paintings, contracts = draw_painting_route(seed), []
            for index, painting in enumerate(paintings):
                result = simulate_painting(deepcopy(participants), painting,
                    seed + 7919 * (index + 1), index + 1, contracts)
                results[f'stage{index + 1}'][result['result']] += 1
                contracts.append(offered_contract(seed, index + 1, contracts))
            for route in ('shadow', 'noah'):
                result = simulate_final_battle(dict(status='running', boss_index=3, seed=seed,
                    paintings=paintings, participants=deepcopy(participants), route=route,
                    contracts=contracts))
                results[route][result['result']] += 1
        rows.append(dict(level=level, players=count, seeds=seeds, seed_start=seed_start,
            accessories=accessories, gear='raid', contract_policy='new_color_then_balanced',
            rate_denominator='independent_full_hp_encounters',
            win_rates={key: value['勝利'] / seeds for key, value in results.items()},
            results={key: dict(value) for key, value in results.items()}))
    return rows


def benchmark(seeds=200, *, counts=(4, 8), level=60, accessories=True, seed_start=0):
    """Actual offered contracts, persistent HP and unconditional complete-run rates."""
    rows = []
    for count in counts:
        participants = build_participants(level, count, accessories=accessories)
        stage_clears = [0, 0, 0]
        failures = Counter()
        wins = Counter()
        final_results = {route: Counter() for route in ('noah', 'shadow')}
        for seed in range(seed_start, seed_start + seeds):
            paintings = draw_painting_route(seed)
            contracts, hp = [], None
            for index, painting in enumerate(paintings):
                result = simulate_painting(deepcopy(participants), painting, seed + 7919 * (index + 1),
                                            index + 1, contracts, hp)
                if result['result'] != '勝利':
                    failures[f'{index + 1}:{painting["base_kind"]}:{result["result"]}'] += 1
                    break
                stage_clears[index] += 1
                hp = result['party_state']
                contracts.append(offered_contract(seed, index + 1, contracts))
            else:
                for route in ('noah', 'shadow'):
                    result = simulate_final_battle(dict(status='running', boss_index=3, seed=seed,
                        paintings=paintings, participants=deepcopy(participants), route=route,
                        contracts=contracts, party_state=deepcopy(hp)))
                    final_results[route][result['result']] += 1
                    wins[route] += result['result'] == '勝利'
        rows.append(dict(level=level, players=count, seeds=seeds, seed_start=seed_start,
            accessories=accessories, gear='raid', contract_policy='new_color_then_balanced',
            rate_denominator='all_entries',
            stage_clears=stage_clears, reach_final_rate=stage_clears[-1] / seeds,
            win_rates={route: wins[route] / seeds for route in final_results},
            final_results={route: dict(results) for route, results in final_results.items()},
            painting_failures=dict(failures)))
    return rows


def simulate_run(level, count, route, contracts, seed, gear='raid'):
    participants = build_participants(level, count, gear)
    paintings = draw_painting_route(seed)
    party_state = None
    painting_rounds = []
    for index, painting in enumerate(paintings):
        active_contracts = contracts[:index]
        result = simulate_painting(
            participants, painting, seed + 7919 * (index + 1), index + 1,
            active_contracts, party_state)
        painting_rounds.append(result['rounds'])
        party_state = result['party_state']
        if result['result'] != '勝利':
            return {'victory': False, 'painting_rounds': painting_rounds,
                    'final_rounds': None, 'failed_at': index + 1}
    room = dict(status='running', boss_index=3, participants=participants,
                party_state=party_state, contracts=list(contracts), route=route, seed=seed)
    final = simulate_final_battle(room)
    return {'victory': final['result'] == '勝利', 'painting_rounds': painting_rounds,
            'final_rounds': final['rounds'], 'failed_at': None if final['result'] == '勝利' else 4}


def simulate_matrix(seeds, levels=(50, 60, 75, 80), counts=(1, 4, 8),
                    routes=('noah', 'shadow'), patterns=None, gear='raid'):
    patterns = patterns or CONTRACT_PATTERNS
    rows = []
    for level in levels:
        for count in counts:
            for route in routes:
                for label, contracts in patterns.items():
                    runs = [simulate_run(level, count, route, contracts, seed, gear)
                            for seed in range(seeds)]
                    stage_rounds = [rounds for run in runs for rounds in run['painting_rounds']]
                    final_rounds = [run['final_rounds'] for run in runs
                                    if run['final_rounds'] is not None]
                    rows.append({
                        'level': level, 'count': count, 'route': route, 'pattern': label,
                        'win_rate': sum(run['victory'] for run in runs) / seeds,
                        'stage_rounds': statistics.mean(stage_rounds) if stage_rounds else 0,
                        'final_rounds': statistics.mean(final_rounds) if final_rounds else 0,
                        'reach_final_rate': len(final_rounds) / seeds,
                    })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=20)
    parser.add_argument('--quick', action='store_true',
                        help='Only run Lv.50/60, 1/8 players and three representative contracts.')
    parser.add_argument('--gear', choices=('raid', 'maze'), default='raid',
                        help='Use current T60 raid gear (default) or cleared Painted Maze gear.')
    parser.add_argument('--benchmark', action='store_true', help='Benchmark Lv.60, 4/8 players with actual contract offers.')
    parser.add_argument('--independent', action='store_true', help='Benchmark eight players at full HP for each encounter.')
    parser.add_argument('--accessories', action='store_true', help='Fill four Lv.60 accessory slots with legal embroidery (benchmark).')
    parser.add_argument('--seed-start', type=int, default=0, help='First deterministic benchmark seed.')
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    if args.benchmark or args.independent:
        import json
        run = independent_benchmark if args.independent else benchmark
        for row in run(args.seeds, accessories=args.accessories, seed_start=args.seed_start):
            print(json.dumps(row, ensure_ascii=False), flush=True)
        return
    kwargs = {}
    if args.quick:
        kwargs.update(levels=(50, 60), counts=(1, 8), patterns={
            key: CONTRACT_PATTERNS[key] for key in ('緋紅三疊', '續航雙色', '三原色')})
    print('等級 人數 路線   契約       通關率 抵達尾王 前置回合 尾王回合')
    for row in simulate_matrix(args.seeds, gear=args.gear, **kwargs):
        print(f'{row["level"]:>4} {row["count"]:>4} {row["route"]:<6} '
              f'{row["pattern"]:<9} {row["win_rate"]:>6.1%} '
              f'{row["reach_final_rate"]:>7.1%} {row["stage_rounds"]:>8.1f} '
              f'{row["final_rounds"]:>8.1f}')


if __name__ == '__main__':
    main()
