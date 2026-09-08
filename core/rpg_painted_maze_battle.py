"""Automatic battle adapter for the nine Painted Maze paintings."""
from collections import Counter

from core.rpg_battle import dump_battle, raid_battle
from core.rpg_monsters import BALANCE_VERSION
from core.rpg_painted_maze import COLOR_CONTRACTS, PaintedMazeError


PAINTING_MAX_ROUNDS = 20
FINAL_MAX_ROUNDS = 30
STAGE_PROFILE = {
    50: dict(tier=5, level_bonus=0, hp=5.8, attack=1.40, defense=1.20,
             speed=52, hit=105, dodge=50, crit=10),
    55: dict(tier=5, level_bonus=5, hp=6.9, attack=1.58, defense=1.30,
             speed=56, hit=107, dodge=56, crit=10),
    60: dict(tier=6, level_bonus=0, hp=8.0, attack=1.75, defense=1.45,
             speed=60, hit=110, dodge=62, crit=12),
}
MULTI_ENEMY_COUNTS = {
    '史萊姆群': 3,
    '哥布林戰團': 3,
    '王城傀儡師': 3,
    '迷霧菌后': 3,
    '赤雷與蒼炎': 2,
    '星蝕巨神': 2,
}


def painting_battle_seed(room_seed, painting_index):
    return room_seed + 7919 * (painting_index + 1)


def party_attack_scale(participant_count):
    """Correct for concentrated enemy actions, capped at +15% for eight players."""
    if participant_count <= 4:
        return (.55, .72, .87, 1.0)[participant_count - 1]
    return 1 + .15 * (participant_count - 4) / 4


def painting_monster(painting, participant_count):
    """Create an isolated profile without inheriting scheduled-raid rewards."""
    if painting.get('tier') not in STAGE_PROFILE:
        raise PaintedMazeError('畫作的內容階級無效。')
    if not 1 <= participant_count <= 8:
        raise PaintedMazeError('繪境迷廊隊伍人數必須為 1～8 人。')
    source = STAGE_PROFILE[painting['tier']]
    # raid_battle applies linear party HP scaling. Counter-adjust the profile to
    # reach 1 + 85% per additional player, as specified by the mode.
    party_hp_scale = (1 + .85 * (participant_count - 1)) / participant_count
    attack_scale = party_attack_scale(participant_count)
    profile = {
        'hp': source['hp'] * party_hp_scale,
        'attack': source['attack'] * attack_scale,
        'defense': source['defense'],
        'speed': source['speed'],
        'level_bonus': source['level_bonus'],
        'hit': source['hit'],
        'dodge': source['dodge'],
        'crit': source['crit'],
        'count': MULTI_ENEMY_COUNTS.get(painting['base_kind'], 1),
    }
    return {
        'name': painting['name'],
        'description': f'畫作甦醒：{painting["name"]}',
        'kind': painting['base_kind'],
        'painting_id': painting['id'],
        'content_level': painting['tier'],
        'balance_version': BALANCE_VERSION,
        'quality': '普通',
        'tier': source['tier'],
        'profile': profile,
        'manual_strength': 1,
        'difficulty_multiplier': 1,
    }


def apply_party_contracts(battle, contracts):
    counts = Counter(contracts)
    for contract_id in counts:
        if contract_id not in COLOR_CONTRACTS:
            raise PaintedMazeError('房間保存了無效的色彩契約。')
    for fighter in (fighter for fighter in battle.fighters if fighter.team == 0):
        if counts['crimson']:
            fighter.status_stacks['maze_direct_damage_percent'] = counts['crimson'] * 8
        fighter.damage_taken_percent -= counts['azure'] * 6
        fighter.speed += counts['gold'] * 8
        if '速度' in fighter.stats:
            fighter.stats['速度'] = fighter.speed
        if counts['gold'] >= 2:
            fighter.cooldown_reduction = max(fighter.cooldown_reduction, 1)
        if counts['verdant']:
            fighter.stats['治療量'] = fighter.stats['治療量'] * (100 + counts['verdant'] * 10) // 100
        if counts['violet']:
            fighter.status_stacks['maze_violet_percent'] = counts['violet'] * 8
        fighter.damage_dealt_percent += counts['black'] * 12
        fighter.damage_taken_percent += counts['black'] * 4
        fighter.stats['暴擊率'] = min(100, fighter.stats['暴擊率'] + counts['black'] * 2)
    battle.mechanics['painted_maze_contracts'] = dict(counts)
    if contracts:
        summary = '、'.join(f'{COLOR_CONTRACTS[key]["name"]} {value} 層'
                           for key, value in counts.items())
        battle.log.append(f'色彩契約生效：{summary}。')
    return battle


def build_painting_battle(participants, painting, seed, contracts=(), carried_hp=None):
    if not participants:
        raise PaintedMazeError('隊伍中沒有可參戰的玩家。')
    monster = painting_monster(painting, len(participants))
    battle = raid_battle(participants, monster, seed)
    battle.max_rounds = PAINTING_MAX_ROUNDS
    apply_party_contracts(battle, contracts)
    carried_hp = carried_hp or {}
    for fighter in (fighter for fighter in battle.fighters if fighter.team == 0):
        saved = carried_hp.get(str(fighter.user_id), carried_hp.get(fighter.user_id))
        if saved is not None:
            current = saved.get('hp', saved) if isinstance(saved, dict) else saved
            fighter.hp = max(0, min(fighter.stats['HP'], int(current)))
    battle.log.insert(0, f'畫作甦醒：{painting["name"]}（T{painting["tier"]}）')
    return battle


def run_automatic_battle(battle):
    while not battle.result:
        battle.step()
    return battle


def carry_party_state(battle, completed_bosses, contracts=()):
    """Seal post-battle HP; transient combat state is intentionally discarded."""
    victory = battle.result == '勝利'
    verdant_layers = Counter(contracts)['verdant']
    party_size = sum(fighter.team == 0 for fighter in battle.fighters)
    recovery = 30 if party_size == 1 else 22 if party_size <= 4 else 15
    curtain_recovery = 50 if party_size == 1 else 40 if party_size <= 4 else 35
    state = {}
    for fighter in (fighter for fighter in battle.fighters if fighter.team == 0):
        maximum = fighter.stats['HP']
        current = fighter.hp
        if victory:
            if current <= 0:
                current = max(1, maximum * 20 // 100)
            else:
                current = min(maximum, current + maximum * (recovery + verdant_layers * 3) // 100)
            if completed_bosses % 3 == 0:
                current = min(maximum, current + maximum * curtain_recovery // 100)
        state[str(fighter.user_id)] = {
            'hp': current,
            'max_hp': maximum,
            'fallen': current <= 0,
        }
    return state


def simulate_painting(participants, painting, seed, completed_bosses,
                      contracts=(), carried_hp=None):
    battle = build_painting_battle(participants, painting, seed, contracts, carried_hp)
    run_automatic_battle(battle)
    return {
        'battle': dump_battle(battle),
        'party_state': carry_party_state(battle, completed_bosses, contracts),
        'result': battle.result,
        'rounds': battle.round,
    }


def simulate_room_painting(room):
    if room.get('status') != 'running':
        raise PaintedMazeError('目前不能挑戰下一幅畫作。')
    painting_index = room.get('boss_index', 0)
    paintings = room.get('paintings', ())
    if painting_index >= len(paintings):
        raise PaintedMazeError('前置畫作已全部完成。')
    return simulate_painting(
        room['participants'], paintings[painting_index],
        painting_battle_seed(room['seed'], painting_index),
        painting_index + 1, room.get('contracts', ()), room.get('party_state'))


def final_monster(room):
    participant_count = len(room.get('participants', ()))
    if not 1 <= participant_count <= 8:
        raise PaintedMazeError('繪境迷廊隊伍人數必須為 1～8 人。')
    route = room.get('route')
    if route not in ('noah', 'shadow'):
        raise PaintedMazeError('繪境迷廊尾王路線無效。')
    hp_base, attack_base, defense = ((10.0, 1.75, 1.65) if route == 'noah'
                                     else (8.0, 1.65, 1.50))
    party_hp_scale = (1 + .85 * (participant_count - 1)) / participant_count
    attack_scale = party_attack_scale(participant_count)
    name = '繪畫魔女．城崎諾亞' if route == 'noah' else '繪畫之影'
    return {
        'name': name, 'description': f'{name} 從完成的構圖中現身。',
        # Reuse Noah's automatic colour-action vocabulary while the outer
        # final-boss mechanics are generated from the selected contracts.
        'kind': '城崎諾亞', 'balance_version': BALANCE_VERSION, 'quality': '普通',
        'tier': 6, 'manual_strength': 1, 'difficulty_multiplier': 1,
        'profile': {
            'hp': hp_base * party_hp_scale, 'attack': attack_base * attack_scale,
            'defense': defense, 'speed': 62, 'level_bonus': 2 if route == 'noah' else 0,
            'hit': 112, 'dodge': 64, 'crit': 14, 'count': 1,
        },
    }


def build_final_battle(room):
    if room.get('status') != 'running' or room.get('boss_index') != 9:
        raise PaintedMazeError('尚未完成九幅前置畫作。')
    participants = room.get('participants', ())
    battle = raid_battle(participants, final_monster(room), room['seed'] + 104729)
    battle.max_rounds = FINAL_MAX_ROUNDS
    contracts = room.get('contracts', ())
    if len(contracts) != 3:
        raise PaintedMazeError('三份色彩契約尚未全部確定。')
    apply_party_contracts(battle, contracts)
    counts = Counter(contracts)
    boss = next(fighter for fighter in battle.fighters if fighter.team == 1 and fighter.is_boss)
    boss.speed += counts['gold'] * 8
    boss.damage_dealt_percent += counts['black'] * 3
    battle.mechanics.update(
        maze_final=True, maze_final_route=room['route'], maze_final_contracts=dict(counts),
        maze_final_skills=list(contracts), maze_final_phase=1,
        maze_final_shield=0, maze_final_phases=[], maze_final_direct_hits=0,
    )
    carried_hp = room.get('party_state', {})
    for fighter in (fighter for fighter in battle.fighters if fighter.team == 0):
        saved = carried_hp.get(str(fighter.user_id), carried_hp.get(fighter.user_id))
        if saved is not None:
            current = saved.get('hp', saved) if isinstance(saved, dict) else saved
            fighter.hp = max(0, min(fighter.stats['HP'], int(current)))
    battle.log.insert(0, f'最終畫室：{boss.name} 現身。')
    return battle


def simulate_final_battle(room):
    battle = build_final_battle(room)
    run_automatic_battle(battle)
    return {
        'battle': dump_battle(battle),
        'party_state': carry_party_state(battle, 10, room.get('contracts', ())),
        'result': battle.result,
        'rounds': battle.round,
    }
