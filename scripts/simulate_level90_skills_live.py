"""Compare equipped Lv.90 skills against legacy loadouts in the production engine.

Run: python -m scripts.simulate_level90_skills_live --seeds 20
"""
import argparse
import json
from pathlib import Path
from statistics import mean
import sys

from core.rpg_monsters import prepare_monster
from scripts.simulate_level90_skills import BASE, CASES, fight


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=20)
    parser.add_argument('--output', default='local_designs/rpg-level90-skills-live.json')
    args = parser.parse_args()
    rows = []
    for party in (list(BASE), list(BASE) + ['裝甲步兵', '弓兵']):
        for kind in ('星蝕巨神', '逆潮聖骸'):
            monster = prepare_monster(dict(kind=kind, name=kind), quality='傳說')
            for job, passive, name, proposal in CASES:
                baseline = (2, 3, 4) if name == '疫羽散射' else BASE[job][passive]
                before = [fight(monster, party, seed, job, passive, baseline)
                          for seed in range(args.seeds)]
                after = [fight(monster, party, seed, job, passive, proposal)
                         for seed in range(args.seeds)]
                average = lambda samples: {key: mean(item[key] for item in samples)
                                           for key in samples[0]}
                rows.append(dict(size=len(party), monster=kind, job=job,
                                 passive=passive, skill=name,
                                 baseline=average(before), proposed=average(after)))
            print(f'Completed {len(party)} players / {kind}', flush=True)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    for name in dict.fromkeys(row['skill'] for row in rows):
        results = [row for row in rows if row['skill'] == name]
        delta = mean(row['proposed']['win'] - row['baseline']['win'] for row in results)
        print(f'{name}: win delta {delta:+.1%}')
    print(path)


if __name__ == '__main__':
    main()
