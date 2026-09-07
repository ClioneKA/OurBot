"""Monster tiers, traits and quality; freeze these values when announcing a raid."""
import random
from decimal import Decimal


BALANCE_VERSION = 6
# Calibrated encounter tiers are content levels. Higher-level players are
# intentionally stronger when returning to these lower-tier encounters.
REFERENCE_LEVELS = {1: 10, 2: 20, 3: 30, 4: 40}
# Base victory XP is tied to encounter tier instead of the channel that hosts
# it. Quality and channel difficulty are applied after this value is selected.
TIER_VICTORY_XP = {0: 1000, 1: 300, 2: 450, 3: 600, 4: 750}
# HP, attack, defense. Tiers are internal and never part of display names.
TIERS = {0: (1, 1, 1), 1: (1, 1, 1), 2: (1.5, 1.2, 1.3), 3: (1.8, 1.15, 1.15),
         4: (2.2, 1.25, 1.2)}
# tier, HP, attack, defense, speed, accuracy, evasion, critical. Speed is an
# absolute initiative value on the same narrow scale as player speed.
PROFILES = {
    # V6 profiles target roughly 15 rounds and an 80% ordinary win rate for a
    # same-tier four-role party that answers the encounter mechanics.
    # Defense is deliberately material enough to separate high and low attack,
    # while multi-enemy encounters split their attack budget across the group.
    '月影妖狐': (2, 3.7392, 1.65, 0.875, 70, 95, 23, 15),
    '血翼蝠王': (2, 3.92, 1.687, 1.0, 65, 94, 21, 10),
    '巨獸': (1, 4.212, 1.9895, 1.0, 40, 88, 10, 10),
    # The spider gives up defense for evasion.  Its larger HP budget keeps a
    # same-tier all-offense archer party from deleting it before round 7.
    '毒蛛': (1, 3.8335, 1.8492, 0.875, 65, 95, 12, 15),
    '史萊姆群': (0, 1.2, 0.8, 0.5, 55, 90, 0, 5),
    '鐵殼魔像': (2, 4.008, 1.3755, 2.2, 35, 90, 18, 5),
    '荊棘妖樹': (2, 3.1668, 2.712, 1.625, 40, 92, 19, 5),
    '哥布林戰團': (2, 5.1624, 1.116, 1.0, 55, 92, 20, 10),
    '深淵鐘龍': (3, 4.7696, 1.57, 1.4375, 45, 92, 30, 10),
    '王城傀儡師': (3, 3.8538, 2.089, 1.125, 55, 94, 31, 10),
    '瘟疫縫合獸': (3, 3.91, 2.691, 1.25, 50, 93, 29, 8),
    # Scheduled tier-four encounters use a T40 reference party. The twins
    # split both their HP and action budget between two bodies.
    '赤雷與蒼炎': (4, 2.94, 1.048, 1.15, 55, 95, 40, 10),
    '吞城鯨': (4, 3.6, 1.537, 1.25, 35, 94, 35, 8),
    # Special summon calibrated around Lv.45 equipment. It is always ordinary
    # quality and is excluded from both scheduled encounter pools.
    '城崎諾亞': (4, 3.803625, 0.9148125, 1.4375, 60, 95, 45, 12),
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
        defense=product(base[2], tdef), speed=speed,
        # Noah remains a fixed T45 special summon while scheduled tier-four
        # encounters use the normal T40 anchor.
        level_bonus=level_bonus + (5 if monster['kind'] == '城崎諾亞' else 0),
        hit=hit, dodge=dodge, crit=crit,
        count=(3 if monster['kind'] in ('史萊姆群', '哥布林戰團', '王城傀儡師') else
               2 if monster['kind'] == '赤雷與蒼炎' else 1)),
        quality_reward=reward, quality_drop=drop)


def monster_name(monster):
    quality = monster.get('quality', '普通')
    return (quality + '・' if quality != '普通' else '') + monster['name']
