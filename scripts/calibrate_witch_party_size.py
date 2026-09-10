"""Screen partial party-size compensation against the four-player calibration."""
import argparse
import json
from pathlib import Path

from core.rpg_witch_scaling import level_scales
from scripts.simulate_witch_full_gear import COMBOS, JOBS, evaluate, party

PARTIES = {
    1: ('騎士',),
    2: ('騎士', '僧侶'),
    3: ('騎士', '弓兵', '僧侶'),
    4: JOBS,
    5: ('裝甲步兵', '騎士', '弓兵', '弓兵', '僧侶'),
    6: ('裝甲步兵', '裝甲步兵', '騎士', '弓兵', '弓兵', '僧侶'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--levels', nargs='+', type=int, default=[50])
    parser.add_argument('--sizes', nargs='+', type=int, default=[1, 2, 3, 4, 5, 6])
    parser.add_argument('--powers', nargs='+', type=float, default=[.25, .4, .55])
    parser.add_argument('--seeds', type=int, default=2)
    parser.add_argument('--seed-start', type=int, default=3000)
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--legacy-size', action='store_true', help='Four-player curve with the previous HP-only size factor.')
    parser.add_argument('--output', type=Path, default=Path('docs/witch-party-size-screen.json'))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = [(ids, seed) for ids in COMBOS for seed in range(args.seed_start, args.seed_start + args.seeds)]
    report = []
    for level in args.levels:
        base = None if args.production else level_scales(level)
        for size in args.sizes:
            participants = party([level] * size, PARTIES[size])
            for exponent in ([None] if args.production or args.legacy_size else args.powers):
                scales = None if args.production else tuple(base) if args.legacy_size else (
                    base[0] * (size / 4) / (.4 + .15 * size),
                    base[1] * (size / 4) ** exponent, base[2])
                result = evaluate(participants, cases, scales)
                row = dict(level=level, size=size, jobs=PARTIES[size], exponent=exponent,
                           seed_start=args.seed_start, seeds=args.seeds,
                           **{k: v for k, v in result.items() if k != 'rows'})
                report.append(row)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
