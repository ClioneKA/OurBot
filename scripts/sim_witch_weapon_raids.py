"""Run complete automatic Witch Rest battles with proposed weapon passives.

The proposal is injected into battle objects in memory; game files and saves
are untouched. Run from the repository root with PYTHONPATH set to that root.
"""
from dataclasses import asdict
from statistics import mean
import sys

from core.rpg_battle import default_rules
from core.rpg_character import (BASE_SPEED, COMBAT_NAMES, CRITICAL_DAMAGE_PERCENT,
                                GROWTH, ITEMS, combat_from_stats)
from core.rpg_witch_rest import WitchRestStore, equipment_id
from core.rpg_witch_rest_battle import auto_battle_from_participants, manual_battle_from_participants
from scripts.sim_witch_weapon_passives import (
    CRYSTAL_PROFILES, OUTPUT_BASE_DAMAGE, OUTPUT_EMA_T80_EXECUTE,
    OUTPUT_HIRO_LOW_HP, RAID_BASE_DAMAGE, make_player,
)


SEEDS = range(1000)


def support_player(job, witch):
    level, stage = 85, 2
    base = tuple(28 + (level - 10) * weight + stage * weight * 2
                 for weight in GROWTH[job])
    stats = combat_from_stats(base, job)
    weapon = ITEMS[equipment_id(witch, job, 'weapon', 80)]
    suit = ITEMS[equipment_id(witch, job, 'suit', 80)]
    for index, name in enumerate(COMBAT_NAMES):
        stats[name] += weapon.combat[index] + suit.combat[index]
    stats['命中率'] += weapon.accuracy
    stats['攻擊'] += sum(WitchRestStore._affix_value(
        equipment_id(witch, job, slot, 80), job, 0, 'assault', 4)
        for slot in ('weapon', 'suit'))
    return {
        'level': level, 'job': job, 'combat': stats,
        'speed': BASE_SPEED[job] + weapon.speed,
        'stability': weapon.stability,
        'critical_damage_percent': CRITICAL_DAMAGE_PERCENT[job] + 8,
        'equipped': {'武器': equipment_id(witch, job, 'weapon', 80),
                     '套裝': equipment_id(witch, job, 'suit', 80)},
    }


def make_participants(witch, tier, variant):
    result = []
    for user_id, job in enumerate(('裝甲步兵', '弓兵', '騎士', '僧侶'), 1):
        if user_id <= 2:
            actor = make_player(job, witch, tier, variant,
                                CRYSTAL_PROFILES['傑作滿結晶'])
            if tier == 70:
                slug = 'infantry' if job == '裝甲步兵' else 'archer'
                weapon_id = f'maze:{slug}:weapon'
                suit_id = f'maze:{slug}:suit'
            else:
                weapon_id = equipment_id(witch, job, 'weapon', tier)
                suit_id = equipment_id(witch, job, 'suit', tier)
            state = {
                'level': 85, 'job': job, 'combat': actor.stats,
                'speed': BASE_SPEED[job] + ITEMS[weapon_id].speed,
                'stability': actor.stability,
                'critical_damage_percent': actor.critical_damage_percent,
                'equipped': {'武器': weapon_id, '套裝': suit_id},
            }
            rules = actor.rules
        else:
            state = support_player(job, witch)
            rules = default_rules(job)
        result.append({'id': user_id, 'name': job, 'state': state,
                       'rules': [asdict(rule) for rule in rules],
                       'basic_target': 'boss', 'passive_id': None})
    return result


def inject_proposal(battle, witch, tier, base_scale=1, proposal='raid'):
    if tier == 70:
        for actor in battle.fighters:
            if actor.team == 0 and actor.user_id in (1, 2):
                source = make_player(actor.job, witch, 70, '現行',
                                     CRYSTAL_PROFILES['傑作滿結晶'])
                actor.status_stacks.update(source.status_stacks)
        return

    begin = battle._begin_passive_action

    def proposed_begin(actor, skill=None, target=None, basic=False):
        context = begin(actor, skill, target, basic)
        if actor.user_id in (1, 2) and context['damaging']:
            bonuses = OUTPUT_BASE_DAMAGE if proposal == 'output' else RAID_BASE_DAMAGE
            context['multiplier'] *= 1 + bonuses[witch][actor.job][tier] * base_scale / 100
            if (proposal == 'output' and witch == 'ema' and tier == 80
                    and target is not None
                    and id(target) in context.get('witch_ema_weapon_targets', ())):
                context['multiplier'] *= (1 + OUTPUT_EMA_T80_EXECUTE / 100) / 1.24
            if (proposal == 'output' and witch == 'hiro' and tier == 80
                    and target is not None
                    and target.hp * 100 <= target.stats['HP'] * 35):
                context['multiplier'] *= 1 + OUTPUT_HIRO_LOW_HP[actor.job] / 100
            if (witch == 'hiro' and skill is not None
                    and actor.passive_state.pop('proposal_hiro_ready', False)):
                context['multiplier'] *= 1.12 if tier == 80 else 1.18
        return context

    battle._begin_passive_action = proposed_begin
    if witch == 'hiro':
        resolve = battle._resolve_skill

        def proposed_resolve(actor, rule, skill, target):
            before = actor.passive_state.get('witch_hiro_weapon_procs', 0)
            result = resolve(actor, rule, skill, target)
            if (actor.user_id in (1, 2)
                    and actor.passive_state.get('witch_hiro_weapon_procs', 0) > before):
                actor.passive_state['proposal_hiro_ready'] = True
            return result

        battle._resolve_skill = proposed_resolve


def run_one(witch, tier, variant, seed, base_scale=1, enrage=99, proposal='raid'):
    participants = make_participants(witch, tier, variant)
    battle = (auto_battle_from_participants(participants, witch, enrage, seed=seed)
              if enrage < 100 else
              manual_battle_from_participants(participants, witch, enrage, seed=seed))
    if variant == '調整版' or tier == 70:
        inject_proposal(battle, witch, tier, base_scale, proposal)
    if enrage >= 100:
        for user_id in battle.living_player_ids():
            battle.enable_auto(user_id)
    while not battle.result:
        if enrage < 100:
            battle.step()
        else:
            battle.resolve()
        # Combat logs are not needed for aggregate statistics.
        battle.log.clear()
    players = sorted((actor for actor in battle.fighters if actor.team == 0),
                     key=lambda actor: actor.user_id)
    return {
        'win': battle.result == '勝利', 'rounds': battle.round,
        'infantry': players[0].combat_stats['direct_damage'],
        'archer': players[1].combat_stats['direct_damage'],
        'team': sum(actor.combat_stats['damage_dealt'] for actor in players),
        'deaths': sum(actor.hp == 0 for actor in players),
        'hiro_procs': sum(actor.passive_state.get('witch_hiro_weapon_procs', 0)
                          for actor in players[:2]),
    }


def main():
    manual = '--manual' in sys.argv
    proposal = 'output' if '--output-proposal' in sys.argv else 'raid'
    seeds = range(500) if manual else SEEDS
    for witch in ('ema', 'hiro'):
        for enrage in ((100, 250) if manual else (99,)):
            for tier, variant in ((70, '現行'), (80, '現行'), (80, '調整版'),
                                  (85, '現行'), (85, '調整版')):
                rows = [run_one(witch, tier, variant, seed, enrage=enrage,
                                proposal=proposal) for seed in seeds]
                wins = [row for row in rows if row['win']]
                print(witch, enrage, f'T{tier}{variant}', {
                    'wins': len(wins), 'total': len(rows),
                    'mean_rounds': round(mean(row['rounds'] for row in rows), 1),
                    'infantry': round(mean(row['infantry'] for row in rows)),
                    'archer': round(mean(row['archer'] for row in rows)),
                    'team': round(mean(row['team'] for row in rows)),
                    'deaths': round(mean(row['deaths'] for row in rows), 2),
                    'hiro_procs': round(mean(row['hiro_procs'] for row in rows), 1),
                })


if __name__ == '__main__':
    main()
