"""Run deterministic raid balance checks against canonical tier parties."""
import argparse
from dataclasses import asdict

from core.rpg_battle import Rule, default_rules, raid_battle
from core.rpg_character import GROWTH, ITEMS, combat_from_stats
from core.rpg_monsters import PROFILES, QUALITIES, REFERENCE_LEVELS, prepare_monster


EQUIPMENT_STAGE = {1: 0, 2: 1}
T30_EQUIPMENT = {
    '裝甲步兵': ('plague:axe', 'clock:infantry'),
    '騎士': ('plague:sword_shield', 'clock:knight'),
    '弓兵': ('plague:bow', 'clock:archer'),
    '僧侶': ('plague:staff', 'clock:monk'),
}
T45_EQUIPMENT = {
    '裝甲步兵': ('noah:infantry:weapon', 'noah:infantry:suit'),
    '騎士': ('noah:knight:weapon', 'noah:knight:suit'),
    '弓兵': ('noah:archer:weapon', 'noah:archer:suit'),
    '僧侶': ('noah:monk:weapon', 'noah:monk:suit'),
}
COMPOSITIONS = {
    'balanced': ('裝甲步兵', '騎士', '弓兵', '僧侶'),
    'no_healer': ('裝甲步兵', '騎士', '弓兵', '弓兵'),
    'no_knight': ('裝甲步兵', '弓兵', '弓兵', '僧侶'),
    'double_monk': ('騎士', '弓兵', '僧侶', '僧侶'),
    'infantry_dps': ('裝甲步兵',) * 4,
    'archer_dps': ('弓兵',) * 4,
    'mixed_dps': ('裝甲步兵', '裝甲步兵', '弓兵', '弓兵'),
}
COMBAT_STATS = ('HP', '攻擊', '防禦', '治療量')


def mechanism_rules(job, kind):
    """Canonical three-skill loadouts that intentionally answer encounter mechanics."""
    if job == '騎士' and kind in ('鐵殼魔像', '城崎諾亞'):
        return [Rule(1, 1, True, 'enemy_charging', 'lowest', skill_id=4),
                Rule(2, 2, True, 'always', 'self', skill_id=1),
                Rule(3, 3, True, 'ally50', 'lowest', skill_id=2, condition_value=50)]
    if job == '僧侶' and kind in ('荊棘妖樹', '王城傀儡師', '瘟疫縫合獸'):
        return [Rule(1, 1, True, 'ally_debuff', 'debuffed', skill_id=3),
                Rule(2, 2, True, 'ally50', 'lowest', skill_id=1, condition_value=50),
                Rule(3, 3, True, 'always', 'strongest', skill_id=2)]
    if job == '裝甲步兵' and kind in ('哥布林戰團', '王城傀儡師'):
        return [Rule(1, 1, True, 'enemies3', 'lowest', skill_id=4, condition_value=3),
                Rule(2, 2, True, 'always', 'lowest', skill_id=2),
                Rule(3, 3, True, 'always', 'lowest', skill_id=1)]
    if job == '弓兵' and kind in ('哥布林戰團', '王城傀儡師'):
        return [Rule(1, 1, True, 'enemies3', 'lowest', skill_id=3, condition_value=3),
                Rule(2, 2, True, 'always', 'lowest', skill_id=1),
                Rule(3, 3, True, 'always', 'lowest', skill_id=2)]
    if job == '弓兵' and kind == '深淵鐘龍':
        return [Rule(1, 1, True, 'always', 'lowest', skill_id=4),
                Rule(2, 2, True, 'always', 'lowest', skill_id=2),
                Rule(3, 3, True, 'always', 'lowest', skill_id=1)]
    if job == '弓兵' and kind == '城崎諾亞':
        return [Rule(1, 1, True, 'always', 'lowest', skill_id=5),
                Rule(2, 2, True, 'always', 'lowest', skill_id=1),
                Rule(3, 3, True, 'always', 'lowest', skill_id=2)]
    return default_rules(job)


def reference_participant(job, tier, user_id, level_bonus=0, kind=None, strategy='mechanics'):
    level = REFERENCE_LEVELS[tier] + level_bonus
    stage = EQUIPMENT_STAGE.get(tier, 1)
    growth = GROWTH[job]
    base = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                 + stage * weight * 2 for weight in growth)
    combat = combat_from_stats(base, job)
    if tier == 4:
        weapon_key, suit_key = T45_EQUIPMENT[job]
        equipped = {'武器': weapon_key, '套裝': suit_key}
    elif tier == 3:
        weapon_key, suit_key = T30_EQUIPMENT[job]
        equipped = {'武器': weapon_key, '套裝': suit_key}
    else:
        equipped = {'武器': f'{job}:{stage}:武器', '套裝': f'{job}:{stage}:套裝'}
    for index, stat in enumerate(COMBAT_STATS):
        combat[stat] += sum(ITEMS[item].combat[index] for item in equipped.values())
    weapon = ITEMS[equipped['武器']]
    suit = ITEMS[equipped['套裝']]
    combat['命中率'] += weapon.accuracy
    combat['閃避率'] += suit.evasion
    state = dict(level=level, job=job, total=base, combat=combat, equipped=equipped,
                 stability=weapon.stability, damage_guard_chance=suit.damage_guard_chance,
                 vulnerable_chance=weapon.vulnerable_chance,
                 vulnerable_percent=weapon.vulnerable_percent)
    rules = mechanism_rules(job, kind) if strategy == 'mechanics' else default_rules(job)
    return dict(id=user_id, name=job, state=state,
                rules=[asdict(rule) for rule in rules])


def reference_party(tier, composition='balanced', level_bonus=0, kind=None, strategy='mechanics'):
    return [reference_participant(job, tier, index, level_bonus, kind, strategy)
            for index, job in enumerate(COMPOSITIONS[composition])]


def simulate(kind, seeds, quality='普通', composition='balanced', profile_scales=None,
             strategy='mechanics'):
    tier = PROFILES[kind][0]
    monster = prepare_monster(dict(kind=kind, name=kind, description='平衡模擬'), quality=quality)
    if profile_scales:
        monster['profile'] = dict(monster['profile'])
        for stat, scale in profile_scales.items():
            monster['profile'][stat] *= scale
    level_bonus = QUALITIES[quality][1]
    party = reference_party(tier, composition, level_bonus, kind, strategy)
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
    level = REFERENCE_LEVELS[tier] + level_bonus
    return dict(kind=kind, tier=tier, level=level, seeds=seeds,
                win_rate=wins / seeds, average_rounds=total_rounds / seeds,
                victory_rounds=winning_rounds / wins if wins else 0,
                timeout_rate=timeouts / seeds, remaining_hp=remaining_hp / seeds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=1000)
    parser.add_argument('--quality', choices=('普通', '精英', '首領', '傳說'), default='普通')
    parser.add_argument('--composition', choices=tuple(COMPOSITIONS), default='balanced')
    parser.add_argument('--strategy', choices=('mechanics', 'default'), default='mechanics')
    parser.add_argument('--hp-scale', type=float, default=1.0)
    parser.add_argument('--attack-scale', type=float, default=1.0)
    parser.add_argument('--defense-scale', type=float, default=1.0)
    calibrated_kinds = tuple(kind for kind, profile in PROFILES.items() if profile[0] in REFERENCE_LEVELS)
    parser.add_argument('kinds', nargs='*', choices=calibrated_kinds)
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    kinds = args.kinds or list(calibrated_kinds)
    if min(args.hp_scale, args.attack_scale, args.defense_scale) <= 0:
        parser.error('profile scales must be positive')
    profile_scales = {'hp': args.hp_scale, 'attack': args.attack_scale,
                      'defense': args.defense_scale}
    print('怪物              階／等級    勝率    平均回合  勝利回合  上限率  剩餘HP')
    for kind in kinds:
        result = simulate(kind, args.seeds, args.quality, args.composition, profile_scales,
                          args.strategy)
        print(f'{kind:<16} T{result["tier"]}/Lv.{result["level"]:<3} '
              f'{result["win_rate"]:>7.1%} {result["average_rounds"]:>9.1f} '
              f'{result["victory_rounds"]:>9.1f} {result["timeout_rate"]:>7.1%} '
              f'{result["remaining_hp"]:>7.1%}')


if __name__ == '__main__':
    main()
