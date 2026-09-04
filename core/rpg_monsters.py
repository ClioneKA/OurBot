"""Monster tiers, traits and quality; freeze these values when announcing a raid."""
import random
from decimal import Decimal


# HP, attack, defense. Tiers are internal and never part of display names.
TIERS = {1: (1, 1, 1), 2: (1.5, 1.2, 1.3)}
# tier, HP, attack, defense, speed, accuracy, evasion, critical
PROFILES = {
    '巨獸': (1, 1.5, 1.2, 0.8, 0.7, 88, 0, 10),
    '毒蛛': (1, 0.8, 0.85, 0.7, 1.4, 95, 15, 15),
    '史萊姆群': (1, 1.2, 0.8, 0.5, 1.1, 90, 8, 5),
    '鐵殼魔像': (2, 1.2, 1.1, 2, 0.5, 90, 0, 5),
    '荊棘妖樹': (2, 1.4, 0.75, 1.3, 0.6, 92, 0, 5),
    '哥布林戰團': (2, 1.2, 0.5, 0.8, 1.1, 92, 8, 10),
}
# probability, HP, attack, defense, victory rewards, equipment drop chance
QUALITIES = {
    '普通': (70, 1, 1, 1, 1, 0.125),
    '精英': (20, 1.5, 1.2, 1.2, 1.5, 0.2),
    '首領': (8, 3, 1.5, 1.5, 3, 0.3),
    '傳說': (2, 5, 2, 2, 5, 0.5),
}


def prepare_monster(monster):
    def product(*values):
        result = Decimal(1)
        for value in values:
            result *= Decimal(str(value))
        return float(result)

    quality = random.choices(tuple(QUALITIES), weights=[q[0] for q in QUALITIES.values()], k=1)[0]
    _, hp, attack, defense, reward, drop = QUALITIES[quality]
    tier, thp, tatk, tdef, speed, hit, dodge, crit = PROFILES[monster['kind']]
    base = TIERS[tier]
    return dict(monster, quality=quality, tier=tier, profile=dict(
        hp=product(base[0], thp, hp), attack=product(base[1], tatk, attack),
        defense=product(base[2], tdef, defense), speed=speed,
        hit=hit, dodge=dodge, crit=crit,
        count=3 if monster['kind'] in ('史萊姆群', '哥布林戰團') else 1),
        quality_reward=reward, quality_drop=drop)


def monster_name(monster):
    quality = monster.get('quality', '普通')
    return (quality + '・' if quality != '普通' else '') + monster['name']
