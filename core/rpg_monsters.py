"""Monster tiers, traits and quality; freeze these values when announcing a raid."""
import random
from decimal import Decimal


BALANCE_VERSION = 3
# Calibrated encounter tiers are content levels. Higher-level players are
# intentionally stronger when returning to these lower-tier encounters.
REFERENCE_LEVELS = {1: 10, 2: 20, 3: 30, 4: 45}
# HP, attack, defense. Tiers are internal and never part of display names.
TIERS = {0: (1, 1, 1), 1: (1, 1, 1), 2: (1.5, 1.2, 1.3), 3: (1.8, 1.15, 1.15),
         4: (2.2, 1.25, 1.2)}
# tier, HP, attack, defense, speed, accuracy, evasion, critical. Speed is an
# absolute initiative value on the same narrow scale as player speed.
PROFILES = {
    # V2 ordinary profiles target roughly 75% wins for a reference party at
    # the tier's content level. Tier 3 additionally allows composition
    # strengths and weaknesses to matter instead of forcing every party to 75%.
    '月影妖狐': (2, 1.6, 1.422, 0.7, 70, 95, 20, 15),
    '血翼蝠王': (2, 1.62, 1.48, 0.8, 65, 94, 12, 10),
    '巨獸': (1, 2.7, 1.68, 0.8, 40, 88, 0, 10),
    # The spider gives up defense for evasion.  Its larger HP budget keeps a
    # same-tier all-offense archer party from deleting it before round 7.
    '毒蛛': (1, 2.05, 1.462, 0.7, 65, 95, 15, 15),
    '史萊姆群': (0, 1.2, 0.8, 0.5, 55, 90, 8, 5),
    '鐵殼魔像': (2, 1.8, 1.54, 2, 35, 90, 0, 5),
    '荊棘妖樹': (2, 1.82, 2.4, 1.3, 40, 92, 0, 5),
    '哥布林戰團': (2, 1.74, 1.2, 0.8, 55, 92, 8, 10),
    '深淵鐘龍': (3, 2.16, 1.35, 1.15, 45, 92, 0, 10),
    '王城傀儡師': (3, 1.47, 1.755, 0.9, 55, 94, 8, 10),
    '瘟疫縫合獸': (3, 1.955, 2.288, 1.0, 50, 93, 3, 8),
    # Special summon calibrated around Lv.45 equipment. It is always ordinary
    # quality and is excluded from both scheduled encounter pools.
    '城崎諾亞': (4, 2.0, 0.65, 1.15, 60, 95, 8, 12),
}
# probability, equivalent level bonus, victory rewards, equipment drop chance
QUALITIES = {
    '普通': (70, 0, 1, 0.125),
    '精英': (20, 5, 1.5, 0.2),
    '首領': (8, 10, 3, 0.3),
    '傳說': (2, 20, 5, 0.5),
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
    _, level_bonus, reward, drop = QUALITIES[quality]
    tier, thp, tatk, tdef, speed, hit, dodge, crit = PROFILES[monster['kind']]
    base = TIERS[tier]
    return dict(monster, balance_version=BALANCE_VERSION, quality=quality, tier=tier, profile=dict(
        hp=product(base[0], thp), attack=product(base[1], tatk),
        defense=product(base[2], tdef), speed=speed, level_bonus=level_bonus,
        hit=hit, dodge=dodge, crit=crit,
        count=3 if monster['kind'] in ('史萊姆群', '哥布林戰團', '王城傀儡師') else 1),
        quality_reward=reward, quality_drop=drop)


def monster_name(monster):
    quality = monster.get('quality', '普通')
    return (quality + '・' if quality != '普通' else '') + monster['name']
