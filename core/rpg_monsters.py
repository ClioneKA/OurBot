"""Monster tiers, traits and quality; freeze these values when announcing a raid."""
import random
from decimal import Decimal


BALANCE_VERSION = 2
# Calibrated encounter tiers are content levels. Higher-level players are
# intentionally stronger when returning to these lower-tier encounters.
REFERENCE_LEVELS = {1: 10, 2: 20, 3: 30}
# HP, attack, defense. Tiers are internal and never part of display names.
TIERS = {0: (1, 1, 1), 1: (1, 1, 1), 2: (1.5, 1.2, 1.3), 3: (1.8, 1.15, 1.15)}
# tier, HP, attack, defense, speed, accuracy, evasion, critical
PROFILES = {
    # V2 ordinary profiles target roughly 75% wins for a reference party at
    # the tier's content level. Tier 3 additionally allows composition
    # strengths and weaknesses to matter instead of forcing every party to 75%.
    '月影妖狐': (2, 1.6, 1.422, 0.7, 1.5, 95, 20, 15),
    '血翼蝠王': (2, 1.55, 1.48, 0.8, 1.3, 94, 12, 10),
    '巨獸': (1, 2.7, 1.68, 0.8, 0.7, 88, 0, 10),
    '毒蛛': (1, 1.6, 1.462, 0.7, 1.4, 95, 15, 15),
    '史萊姆群': (0, 1.2, 0.8, 0.5, 1.1, 90, 8, 5),
    '鐵殼魔像': (2, 1.8, 1.54, 2, 0.5, 90, 0, 5),
    '荊棘妖樹': (2, 1.82, 2.4, 1.3, 0.6, 92, 0, 5),
    '哥布林戰團': (2, 1.74, 1.2, 0.8, 1.1, 92, 8, 10),
    '深淵鐘龍': (3, 2.16, 1.35, 1.15, 0.75, 92, 0, 10),
    '王城傀儡師': (3, 1.47, 1.755, 0.9, 1.0, 94, 8, 10),
    '瘟疫縫合獸': (3, 1.955, 2.288, 1.0, 0.9, 93, 3, 8),
}
# probability, HP, attack, defense, victory rewards, equipment drop chance
QUALITIES = {
    '普通': (70, 1, 1, 1, 1, 0.125),
    # The ordinary profiles already sit near the party survival threshold.
    # Quality therefore adds endurance without multiplying lethal attack and
    # damage-suppressing defense a second time.
    '精英': (20, 1.07, 1, 1, 1.5, 0.2),
    '首領': (8, 1.13, 1, 1, 3, 0.3),
    '傳說': (2, 1.2, 1, 1, 5, 0.5),
}


def prepare_monster(monster, quality=None):
    def product(*values):
        result = Decimal(1)
        for value in values:
            result *= Decimal(str(value))
        return float(result)

    if quality is None:
        quality = random.choices(tuple(QUALITIES), weights=[q[0] for q in QUALITIES.values()], k=1)[0]
    elif quality not in QUALITIES:
        raise ValueError('Unknown monster quality.')
    _, hp, attack, defense, reward, drop = QUALITIES[quality]
    tier, thp, tatk, tdef, speed, hit, dodge, crit = PROFILES[monster['kind']]
    base = TIERS[tier]
    return dict(monster, balance_version=BALANCE_VERSION, quality=quality, tier=tier, profile=dict(
        hp=product(base[0], thp, hp), attack=product(base[1], tatk, attack),
        defense=product(base[2], tdef, defense), speed=speed,
        hit=hit, dodge=dodge, crit=crit,
        count=3 if monster['kind'] in ('史萊姆群', '哥布林戰團', '王城傀儡師') else 1),
        quality_reward=reward, quality_drop=drop)


def monster_name(monster):
    quality = monster.get('quality', '普通')
    return (quality + '・' if quality != '普通' else '') + monster['name']
