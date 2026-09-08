"""Deterministic end-to-end balance matrix for the Painted Maze.

The simulator uses real character snapshots and the equipment players are
expected to own at entry.  It intentionally runs the entire nine-painting
route with carried HP instead of treating each boss as a fresh raid.
"""
import argparse
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
from core.rpg_painted_maze import draw_painting_route
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


def build_participants(level, count, gear='raid'):
    directory = tempfile.TemporaryDirectory()
    store = RPGStore(Path(directory.name) / 'maze-balance.db')
    characters = Characters(store, RPGSettings())
    equipment = (T50_EQUIPMENT if level < 60 else
                 T60_MAZE_EQUIPMENT if gear == 'maze' else T60_RAID_EQUIPMENT)
    participants = []
    for index in range(count):
        user_id, job = index + 1, JOBS[index % len(JOBS)]
        store.award_voice([(1, user_id, level_floor(level))])
        characters.change_job(1, user_id, job)
        for item_id in equipment[job]:
            characters.grant_item(1, user_id, item_id)
            characters.equip(1, user_id, item_id)
        participants.append({
            'id': user_id, 'name': f'{job}{index + 1}',
            'state': characters.snapshot(1, user_id),
            'rules': [asdict(item) for item in maze_rules(job)],
            'passive_id': 1,
        })
    store.close()
    directory.cleanup()
    return participants


def simulate_run(level, count, route, contracts, seed, gear='raid'):
    participants = build_participants(level, count, gear)
    paintings = draw_painting_route(seed)
    party_state = None
    painting_rounds = []
    for index, painting in enumerate(paintings):
        active_contracts = contracts[:index // 3]
        result = simulate_painting(
            participants, painting, seed + 7919 * (index + 1), index + 1,
            active_contracts, party_state)
        painting_rounds.append(result['rounds'])
        party_state = result['party_state']
        if result['result'] != '勝利':
            return {'victory': False, 'painting_rounds': painting_rounds,
                    'final_rounds': None, 'failed_at': index + 1}
    room = dict(status='running', boss_index=9, participants=participants,
                party_state=party_state, contracts=list(contracts), route=route, seed=seed)
    final = simulate_final_battle(room)
    return {'victory': final['result'] == '勝利', 'painting_rounds': painting_rounds,
            'final_rounds': final['rounds'], 'failed_at': None if final['result'] == '勝利' else 10}


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
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
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
