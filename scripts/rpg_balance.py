"""Run deterministic raid balance checks against canonical tier parties."""
import argparse
from dataclasses import asdict

from core.rpg_battle import default_rules, raid_battle
from core.rpg_character import GROWTH, ITEMS, combat_from_stats
from core.rpg_monsters import PROFILES, REFERENCE_LEVELS, prepare_monster


EQUIPMENT_STAGE = {1: 0, 2: 1}
T30_EQUIPMENT = {
    '裝甲步兵': ('plague:axe', 'clock:infantry'),
    '騎士': ('plague:sword_shield', 'clock:knight'),
    '弓兵': ('plague:bow', 'clock:archer'),
    '僧侶': ('plague:staff', 'clock:monk'),
}
COMPOSITIONS = {
    'balanced': ('裝甲步兵', '騎士', '弓兵', '僧侶'),
    'no_healer': ('裝甲步兵', '騎士', '弓兵', '弓兵'),
    'no_knight': ('裝甲步兵', '弓兵', '弓兵', '僧侶'),
    'double_monk': ('騎士', '弓兵', '僧侶', '僧侶'),
}
COMBAT_STATS = ('HP', '攻擊', '防禦', '治療量')


def reference_participant(job, tier, user_id):
    level = REFERENCE_LEVELS[tier]
    stage = EQUIPMENT_STAGE.get(tier, 1)
    growth = GROWTH[job]
    base = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                 + stage * weight * 2 for weight in growth)
    combat = combat_from_stats(base)
    if tier == 3:
        weapon_key, suit_key = T30_EQUIPMENT[job]
        equipped = {'武器': weapon_key, '套裝': suit_key}
    else:
        equipped = {'武器': f'{job}:{stage}:武器', '套裝': f'{job}:{stage}:套裝'}
    for index, stat in enumerate(COMBAT_STATS):
        combat[stat] += sum(ITEMS[item].combat[index] for item in equipped.values())
    weapon = ITEMS[equipped['武器']]
    suit = ITEMS[equipped['套裝']]
    state = dict(level=level, job=job, total=base, combat=combat, equipped=equipped,
                 stability=weapon.stability, damage_guard_chance=suit.damage_guard_chance,
                 vulnerable_chance=weapon.vulnerable_chance,
                 vulnerable_percent=weapon.vulnerable_percent)
    return dict(id=user_id, name=job, state=state,
                rules=[asdict(rule) for rule in default_rules(job)])


def reference_party(tier, composition='balanced'):
    return [reference_participant(job, tier, index)
            for index, job in enumerate(COMPOSITIONS[composition])]


def simulate(kind, seeds, quality='普通', composition='balanced'):
    tier = PROFILES[kind][0]
    monster = prepare_monster(dict(kind=kind, name=kind, description='平衡模擬'), quality=quality)
    party = reference_party(tier, composition)
    wins = timeouts = total_rounds = winning_rounds = remaining_hp = 0
    for seed in range(seeds):
        battle = raid_battle(party, monster, seed)
        while not battle.result:
            battle.step()
        victory = battle.result == '勝利'
        wins += victory
        timeouts += '回合上限' in battle.result
        total_rounds += battle.round
        winning_rounds += battle.round if victory else 0
        enemies = [fighter for fighter in battle.fighters if fighter.team == 1]
        maximum = sum(fighter.stats['HP'] for fighter in enemies)
        remaining_hp += sum(max(0, fighter.hp) for fighter in enemies) / maximum if maximum else 0
    return dict(kind=kind, tier=tier, level=REFERENCE_LEVELS[tier], seeds=seeds,
                win_rate=wins / seeds, average_rounds=total_rounds / seeds,
                victory_rounds=winning_rounds / wins if wins else 0,
                timeout_rate=timeouts / seeds, remaining_hp=remaining_hp / seeds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=1000)
    parser.add_argument('--quality', choices=('普通', '精英', '首領', '傳說'), default='普通')
    parser.add_argument('--composition', choices=tuple(COMPOSITIONS), default='balanced')
    calibrated_kinds = tuple(kind for kind, profile in PROFILES.items() if profile[0] in REFERENCE_LEVELS)
    parser.add_argument('kinds', nargs='*', choices=calibrated_kinds)
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    kinds = args.kinds or list(calibrated_kinds)
    print('怪物              階／等級    勝率    平均回合  勝利回合  上限率  剩餘HP')
    for kind in kinds:
        result = simulate(kind, args.seeds, args.quality, args.composition)
        print(f'{kind:<16} T{result["tier"]}/Lv.{result["level"]:<3} '
              f'{result["win_rate"]:>7.1%} {result["average_rounds"]:>9.1f} '
              f'{result["victory_rounds"]:>9.1f} {result["timeout_rate"]:>7.1%} '
              f'{result["remaining_hp"]:>7.1%}')


if __name__ == '__main__':
    main()
