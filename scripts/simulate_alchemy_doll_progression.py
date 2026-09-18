"""Sample body rolls, life-work thresholds, gacha pity, and core drops."""
import argparse
import math
import random
from collections import Counter

from core.settings import RPGSettings
from scripts.simulate_alchemy_dolls import BODY_BUDGETS, JOBS, player_growth_stats


RARITIES = (('普通', 0.70, 0.70), ('稀有', 0.24, 0.80),
            ('史詩', 0.05, 0.90), ('傳說', 0.01, 1.00))
LIFE_PAIRS = {'釣魚': (3, 2), '農耕': (1, 0), '料理': (3, 4), '討伐響應': (3, 0)}


def stat_caps(level):
    settings = RPGSettings()
    return tuple(max(player_growth_stats(job, level, settings)[index] for job in JOBS)
                 for index in range(5))


def roll_body(level, pair, rng):
    caps = stat_caps(level)
    weights = [0.10] * 5
    for index in pair:
        weights[index] += 0.25
    stats = [0] * 5
    for _ in range(BODY_BUDGETS[level]):
        available = [index for index in range(5) if stats[index] < caps[index]]
        roll = rng.random() * sum(weights[index] for index in available)
        for index in available:
            roll -= weights[index]
            if roll < 0:
                stats[index] += 1
                break
    return tuple(stats)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def life_samples(samples, rng):
    print('生活工作力（普通技能石；P10 / 中位 / P90）')
    for level in (10, 20, 30, 40, 50, 60, 80, 100):
        fields = []
        for name, (main, secondary) in LIFE_PAIRS.items():
            values = []
            for _ in range(samples):
                stats = roll_body(level, (main, secondary), rng)
                values.append(math.floor((stats[main] * 2 + stats[secondary]) / 3 * 0.70))
            fields.append(f'{name} {percentile(values, .1)}/{percentile(values, .5)}/{percentile(values, .9)}')
        print(f'T{level}: ' + '｜'.join(fields))


def gacha_samples(samples, rng):
    totals = Counter()
    first_epic, first_legend = [], []
    for _ in range(samples):
        epic_misses = legend_misses = 0
        epic_at = legend_at = None
        for pull in range(1, 102):
            if legend_misses >= 99:
                rarity = 3
            elif epic_misses >= 49:
                rarity = 3 if rng.random() < 1 / 6 else 2
            else:
                roll = rng.random()
                rarity = 0 if roll < .70 else 1 if roll < .94 else 2 if roll < .99 else 3
            totals[rarity] += 1
            epic_misses = 0 if rarity >= 2 else epic_misses + 1
            legend_misses = 0 if rarity == 3 else legend_misses + 1
            if epic_at is None and rarity >= 2:
                epic_at = pull
            if rarity == 3:
                legend_at = pull
                break
        first_epic.append(epic_at)
        first_legend.append(legend_at)
    count = sum(totals.values())
    print('\n技能石單抽與保底')
    print('長期稀有度：' + '｜'.join(
        f'{RARITIES[index][0]} {totals[index] / count:.2%}' for index in range(4)))
    for label, values in (('首次史詩+', first_epic), ('首次傳說', first_legend)):
        print(f'{label}：平均 {sum(values) / len(values):.1f} 抽｜'
              f'中位 {percentile(values, .5)}｜P90 {percentile(values, .9)}｜'
              f'最遲 {max(values)}')


def progression_tables():
    print('\n耐久與燃料（耐久專精配方的名目值）')
    for level in BODY_BUDGETS:
        durability = stat_caps(level)[2]
        discount = min(40, durability // 5)
        print(f'T{level}: 耐久 {durability}｜折扣 {discount}%｜'
              f'單操作 {100 - discount} 燃料（約 {(100 - discount) / 4:.2f} 金幣價值）')
    print('\n核心掉落')
    for label, chance, cost in (('Lv.2 繪境', .10, 0), ('Lv.3 1000%', .30, 10)):
        mean = 1 / chance
        median = math.ceil(math.log(.5) / math.log(1 - chance))
        p90 = math.ceil(math.log(.1) / math.log(1 - chance))
        extra = f'｜平均消耗 {mean * cost:.1f} 證' if cost else ''
        print(f'{label}: 平均 {mean:.1f} 次｜中位 {median}｜P90 {p90}{extra}')


def self_check():
    rng = random.Random(1)
    assert roll_body(10, (0, 1), rng) == (28, 28, 28, 28, 28)
    assert sum(roll_body(60, (3, 2), rng)) == BODY_BUDGETS[60]
    assert stat_caps(60)[2] == 190


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=2_000)
    parser.add_argument('--seed', type=int, default=20260918)
    args = parser.parse_args()
    if args.samples < 100:
        parser.error('--samples must be at least 100')
    self_check()
    rng = random.Random(args.seed)
    life_samples(args.samples, rng)
    gacha_samples(args.samples, rng)
    progression_tables()


if __name__ == '__main__':
    main()
