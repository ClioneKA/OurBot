"""Compare geared players with same-tier naked alchemy-doll proxies."""
import argparse
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import tempfile

from core.rpg import RPGStore, level_floor
from core.rpg_battle import raid_battle
from core.rpg_character import Characters, GROWTH, stage_for
from core.rpg_monsters import PROFILES, REFERENCE_LEVELS, prepare_monster
from core.settings import RPGSettings
from scripts.rpg_balance import T30_EQUIPMENT, T40_EQUIPMENT, mechanism_rules
from scripts.simulate_tier56 import SETS as HIGH_TIER_EQUIPMENT, tactics as high_tier_tactics


JOBS = ('裝甲步兵', '騎士', '弓兵', '僧侶')
BODY_BUDGETS = {10: 140, 20: 260, 30: 360, 40: 460, 50: 580, 60: 680,
                70: 780, 80: 880, 90: 1000, 100: 1100, 110: 1200}
# A fully focused recipe sends 50% of its rolls to the named pair and keeps
# a 50% universal floor.  These are recipe examples, not classes.
RECIPE_PAIRS = {
    '裝甲步兵': (0, 1),  # structure / power
    '騎士': (0, 2),      # structure / durability
    '弓兵': (1, 3),      # power / precision
    '僧侶': (2, 4),      # durability / spirit
}
WEAPON_ACCURACY_BY_TIER = {
    10: 10,
    20: 20,
    30: 30,
    40: 40,
    50: 60,
    60: 60,
    70: 70,
    80: 80,
    90: 90,
    100: 100,
    110: 110,
}


def player_growth_stats(job, level, settings):
    stage = stage_for(level, settings)
    return tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                 + stage * weight * 2 for weight in GROWTH[job])


def recipe_stats(job, level):
    budget = BODY_BUDGETS[level]
    pair = RECIPE_PAIRS[job]
    weights = [0.10] * 5
    for index in pair:
        weights[index] += 0.25
    settings = RPGSettings()
    caps = [max(player_growth_stats(candidate, level, settings)[index] for candidate in JOBS)
            for index in range(5)]
    result = [0] * 5
    remaining, active = budget, set(range(5))
    while active:
        weight_total = sum(weights[index] for index in active)
        capped = [index for index in active
                  if remaining * weights[index] / weight_total >= caps[index]]
        if not capped:
            raw = {index: remaining * weights[index] / weight_total for index in active}
            for index in active:
                result[index] = int(raw[index])
            leftover = budget - sum(result)
            for index in sorted(active, key=lambda i: raw[i] - result[i], reverse=True)[:leftover]:
                result[index] += 1
            break
        for index in capped:
            result[index] = caps[index]
            remaining -= caps[index]
            active.remove(index)
    return tuple(result)


def doll_participant(job, level, user_id, kind, body_model='recipes'):
    total = (recipe_stats(job, level) if body_model == 'recipes'
             else player_growth_stats(job, level, RPGSettings()))
    structure, power, durability, precision, spirit = total
    combat = {
        'HP': 50 + structure * 10,
        '攻擊': power * 3,
        '防禦': durability * 3,
        '治療量': spirit * 3,
        '命中率': 95 + WEAPON_ACCURACY_BY_TIER[level],
        '閃避率': 0,
        '暴擊率': 10,
    }
    rules = high_tier_tactics(kind, job) if level >= 50 else mechanism_rules(job, kind)
    return {
        'id': user_id,
        'name': f'{job}人偶',
        'state': {
            'level': level,
            'job': job,
            'total': total,
            'combat': combat,
            'speed': min(100, 35 + precision // 10),
            'equipped': {'武器': '__alchemy_doll__'},
            'stability': (100, 100),
        },
        'rules': [asdict(rule) for rule in rules],
    }


def equipment_for(tier, job):
    if tier == 1:
        return (f'{job}:0:武器', f'{job}:0:套裝')
    if tier == 2:
        return (f'{job}:1:武器', f'{job}:1:套裝')
    if tier == 3:
        return T30_EQUIPMENT[job]
    if tier == 4:
        return T40_EQUIPMENT[job]
    return HIGH_TIER_EQUIPMENT[tier][job]


def player_parties():
    directory = tempfile.TemporaryDirectory()
    store = RPGStore(Path(directory.name) / 'alchemy-doll-balance.db')
    characters = Characters(store, RPGSettings())
    parties = {}
    try:
        for tier, level in REFERENCE_LEVELS.items():
            party = []
            for index, job in enumerate(JOBS, 1):
                user_id = tier * 10 + index
                store.award_voice([(1, user_id, level_floor(level))])
                characters.change_job(1, user_id, job)
                for item_id in equipment_for(tier, job):
                    characters.grant_item(1, user_id, item_id)
                    characters.equip(1, user_id, item_id)
                party.append(characters.snapshot(1, user_id))
            parties[tier] = party
    finally:
        store.close()
        directory.cleanup()
    return parties


def scenarios(player_states, tier, level, kind, body_model):
    players = []
    for index, (job, state) in enumerate(zip(JOBS, player_states), 1):
        rules = high_tier_tactics(kind, job) if tier >= 5 else mechanism_rules(job, kind)
        players.append({
            'id': tier * 10 + index,
            'name': job,
            'state': state,
            'passive_id': 1,
            'rules': [asdict(rule) for rule in rules],
        })
    dolls = [doll_participant(job, level, 1000 + index, kind, body_model)
             for index, job in enumerate(JOBS, 1)]
    return {
        '4P': (players, 4),
        '3P': (players[:3], 3),
        '3P+1D': (players[:3] + dolls[3:], 3),
        '2P': ([players[0], players[2]], 2),
        '2P+2D': ([players[0], players[2], dolls[1], dolls[3]], 2),
        '1P': ([players[0]], 1),
        '1P+1D': ([players[0], dolls[3]], 1),
    }


def run(samples, doll_hp_weight, body_model):
    parties = player_parties()
    rows = []
    for kind, profile in PROFILES.items():
        tier = profile[0]
        if tier not in REFERENCE_LEVELS or kind == '城崎諾亞':
            continue
        level = REFERENCE_LEVELS[tier]
        for label, (participants, humans) in scenarios(
                parties[tier], tier, level, kind, body_model).items():
            wins = rounds = survivors = 0
            for seed in range(samples):
                monster = prepare_monster({'kind': kind, 'name': kind}, '普通')
                effective_size = humans + (len(participants) - humans) * doll_hp_weight
                monster['profile'] = dict(monster['profile'])
                monster['profile']['hp'] *= effective_size / len(participants)
                battle = raid_battle(deepcopy(participants), monster, seed)
                while battle.result is None:
                    battle.step()
                wins += battle.result == '勝利'
                rounds += battle.round
                survivors += sum(fighter.hp > 0 for fighter in battle.fighters[:len(participants)])
            rows.append((tier, kind, label, wins / samples, rounds / samples,
                         survivors / samples))
    return rows


def self_check():
    level = 60
    doll = doll_participant('裝甲步兵', level, 1, '星蝕巨神')
    assert sum(doll['state']['total']) == 680
    assert doll['state']['combat']['HP'] > 0
    assert doll['state']['combat']['命中率'] == 155
    assert doll['state']['equipped']['武器']
    assert sum(recipe_stats('弓兵', 60)) == BODY_BUDGETS[60]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=300)
    parser.add_argument('--doll-hp-weight', type=float, default=1.0)
    parser.add_argument('--body-model', choices=('recipes', 'player-growth'), default='recipes')
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    if not 0 <= args.doll_hp_weight <= 1:
        parser.error('--doll-hp-weight must be between 0 and 1')
    self_check()
    rows = run(args.samples, args.doll_hp_weight, args.body_model)
    if args.summary:
        print('Tier 隊伍       平均勝率')
        for tier in sorted({row[0] for row in rows}):
            tier_rows = [row for row in rows if row[0] == tier]
            for label in ('4P', '3P', '3P+1D', '2P', '2P+2D', '1P', '1P+1D'):
                rates = [row[3] for row in tier_rows if row[2] == label]
                print(f'T{tier:<3} {label:<7} {sum(rates) / len(rates):>8.1%}')
        return
    print('Tier 怪物             隊伍       勝率   平均回合  平均存活')
    for tier, kind, label, win_rate, rounds, survivors in rows:
        print(f'T{tier}   {kind:<16} {label:<7} {win_rate:>6.1%} {rounds:>9.1f} {survivors:>9.2f}')


if __name__ == '__main__':
    main()
