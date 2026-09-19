"""Monte Carlo benchmark for a mechanic-aware Lv.80 T70 party.

This is deliberately a synthetic ceiling for pre-Witch-Rest equipment. It uses
four different jobs, max-roll masterpiece crystals, no consumables, dolls,
divination cards, or Witch Rest equipment, and scripted mechanic resolution.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from itertools import product
import argparse
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from core.rpg_battle import Rule
from core.rpg_character import CRITICAL_DAMAGE_PERCENT, ITEMS, combat_from_stats
from core.rpg_total_battle import ACTION_ATTACK, ACTION_SKILL
from core.rpg_witch_battle import ACTION_DEFEND
from core.rpg_witch_rest_battle import manual_battle_from_participants


JOBS = ('裝甲步兵', '騎士', '弓兵', '僧侶')
ENRAGES = (100, 250, 500, 750, 1_000, 2_000, 4_000)
BASE = {
    '裝甲步兵': (250, 250, 176, 102, 102),
    '騎士': (324, 102, 250, 102, 102),
    '弓兵': (176, 250, 102, 250, 102),
    '僧侶': (176, 102, 102, 176, 324),
}
RULES = {
    '裝甲步兵': (2, 1, 5),       # break, strike, crush
    '騎士': (1, 2, 3),                 # taunt, guard, charge
    '弓兵': (1, 4, 5),                 # double, triple, poison
    '僧侶': (1, 3, 4),                 # heal, cleanse, group heal
}
PASSIVES = {'裝甲步兵': 1, '騎士': 2, '弓兵': 1, '僧侶': 1}
SOURCE_EFFECTS = {
    '裝甲步兵': (('tempered_embers', 8), ('formation_breaker', 8)),
    '騎士': (('guardian_oath', 12), ('life_lance', 12)),
    '弓兵': (('endless_arrow', 7), ('focused_shot', 7)),
    '僧侶': (('grace_reserve', 12), ('holy_afterglow', 12)),
}


def participant(user_id: int, job: str) -> dict:
    weapon = ITEMS[f'maze:{ {"\u88dd\u7532\u6b65\u5175":"infantry", "\u9a0e\u58eb":"knight", "\u5f13\u5175":"archer", "\u50e7\u4fb6":"monk"}[job]}:weapon']
    suit = ITEMS[f'maze:{ {"\u88dd\u7532\u6b65\u5175":"infantry", "\u9a0e\u58eb":"knight", "\u5f13\u5175":"archer", "\u50e7\u4fb6":"monk"}[job]}:suit']
    combat = combat_from_stats(BASE[job], job)
    for index, name in enumerate(('HP', '攻擊', '防禦', '治療量')):
        combat[name] += weapon.combat[index] + suit.combat[index]
    # Two max masterpiece outline crystals. DPS jobs use attack; the monk uses healing.
    combat['治療量' if job == '僧侶' else '攻擊'] += 60
    combat['命中率'] += weapon.accuracy
    combat['速度'] = min(100, {'裝甲步兵': 45, '騎士': 40, '弓兵': 60, '僧侶': 50}[job] + weapon.speed)
    crystals = [
        {'effects': ('high_hp_damage_percent',), 'values': (12,)},
        {'effects': ('survive_fatal_once',), 'values': (1,)},
        *({'effects': (effect,), 'values': (value,)} for effect, value in SOURCE_EFFECTS[job]),
    ]
    rules = [Rule(slot, slot, True, 'always', 'boss', skill_id=skill_id)
             for slot, skill_id in enumerate(RULES[job], 1)]
    state = dict(level=80, job=job, combat=combat, speed=combat['速度'],
                 stability=weapon.stability,
                 equipped={'武器': f'maze:{job}:weapon', '套裝': f'maze:{job}:suit'},
                 crystal_effects=crystals,
                 critical_damage_percent=CRITICAL_DAMAGE_PERCENT[job])
    return dict(id=user_id, name=job, state=state, rules=[asdict(rule) for rule in rules],
                basic_target='mechanic', passive_id=PASSIVES[job])


PARTY = [participant(index, job) for index, job in enumerate(JOBS, 1)]


def target_key(battle, actor, action, slot=None, preferred=None):
    valid = battle.valid_targets(actor.user_id, action, slot)
    if preferred in valid:
        return preferred
    if not valid:
        return None
    enemies = [key for key in valid if battle.fighter_for_key(key).team == 1]
    pool = enemies or valid
    return min(pool, key=lambda key: battle.fighter_for_key(key).hp /
               battle.fighter_for_key(key).stats['HP'])


def ready_slots(battle, actor):
    return [item['skill_slot'] for item in battle.available_actions(actor.user_id)
            if item['action'] == ACTION_SKILL and not item.get('cooldown_remaining')]


def submit(battle, actor, category, preferred=None, preferred_slot=None):
    if category == 'defend':
        return battle.submit(actor.user_id, ACTION_DEFEND)
    if category == 'skill':
        slots = ready_slots(battle, actor)
        slot = preferred_slot if preferred_slot in slots else (slots[0] if slots else None)
        if slot is not None:
            target = target_key(battle, actor, ACTION_SKILL, slot, preferred)
            return battle.submit(actor.user_id, ACTION_SKILL, target, slot)
    target = target_key(battle, actor, ACTION_ATTACK, preferred=preferred)
    return battle.submit(actor.user_id, ACTION_ATTACK, target)


def solve_ema_final(battle, players, final):
    manifests = list(final['manifests'])
    if not manifests:
        for player in players:
            choose_normal(battle, player)
        return
    shadows = {owner: key for key, owner in final['shadows'].items()
               if battle.fighter_for_key(key).hp > 0}
    solvable = [user_id for user_id in manifests if user_id in shadows]
    if not solvable:
        for player in players:
            choose_normal(battle, player)
        return
    defender_ids = solvable[:max(1, (len(players) + 1) // 2)]
    defenders = set(defender_ids)
    attackers = [player for player in players if player.user_id not in defenders]
    for player in players:
        if player.user_id in defenders:
            submit(battle, player, 'defend')
        else:
            owner = defender_ids[attackers.index(player) % len(defender_ids)]
            submit(battle, player, 'attack', shadows[owner])


def solve_hiro_final(battle, players, final):
    step = final['step']
    categories = ('attack', 'skill', 'defend')
    chosen = None
    for candidate in product(categories, repeat=len(players)):
        if set(candidate) != set(categories):
            continue
        legal = True
        for player, category in zip(players, candidate):
            reference = final['reference'].get(str(player.user_id), [])
            if step < len(reference) and category == reference[step]:
                legal = False
            if (player.user_id in final.get('focus', []) and step > 0
                    and category == final['previous'].get(str(player.user_id))):
                legal = False
            if category == 'skill' and not ready_slots(battle, player):
                legal = False
        if legal:
            chosen = candidate
            break
    # Rewrites exist specifically as an escape hatch if cooldowns make the grid impossible.
    if chosen is None:
        chosen = ('attack', 'skill', 'defend', 'attack')[:len(players)]
        rewrites = final.get('rewrite_used', [])
        for player, category in zip(players, chosen):
            action = f'rewrite_{category}'
            if player.user_id not in rewrites:
                battle.submit(player.user_id, action)
            else:
                submit(battle, player, category)
        return
    for player, category in zip(players, chosen):
        submit(battle, player, category)


def choose_normal(battle, player, preferred=None):
    ready = ready_slots(battle, player)
    job = player.job
    if job == '僧侶':
        debuffed = [ally for ally in battle.living(0)
                    if any(ally.status_stacks.get(key) for key in ('factor', 'poison_arrow'))
                    or any(ally.has(key, battle.planning_round) for key in ('stun', 'weak', 'break', 'poison'))]
        injured = [ally for ally in battle.living(0) if ally.hp < ally.stats['HP'] * .7]
        if debuffed and 2 in ready:
            return submit(battle, player, 'skill', battle.key(debuffed[0]), 2)
        if len(injured) >= 2 and 3 in ready:
            return submit(battle, player, 'skill', preferred_slot=3)
        if injured and 1 in ready:
            return submit(battle, player, 'skill', battle.key(min(injured, key=lambda x: x.hp / x.stats['HP'])), 1)
        return submit(battle, player, 'attack', preferred)
    priorities = {'裝甲步兵': (1, 3, 2), '騎士': (2, 1, 3), '弓兵': (2, 3, 1)}[job]
    slot = next((value for value in priorities if value in ready), None)
    if slot is not None:
        return submit(battle, player, 'skill', preferred, slot)
    return submit(battle, player, 'attack', preferred)


def plan_round(battle):
    players = sorted(battle.living(0), key=lambda item: item.user_id)
    if not players:
        return
    ema_final = battle.mechanics.get('ema_final')
    if ema_final and ema_final.get('active'):
        return solve_ema_final(battle, players, ema_final)
    hiro_final = battle.mechanics.get('hiro_final')
    if hiro_final and hiro_final.get('active'):
        return solve_hiro_final(battle, players, hiro_final)
    pending = battle.pending.get('ema')
    if pending and pending.get('kind') == 'rest_guilt':
        defendants = {battle.fighter_for_key(key).user_id for key in pending['targets']}
        assignments = {uid: obj for obj, users in pending['assignments'].items() for uid in users}
        for player in players:
            if player.user_id in defendants:
                submit(battle, player, 'defend')
            elif player.user_id in assignments:
                submit(battle, player, 'attack', assignments[player.user_id])
            else:
                choose_normal(battle, player)
        return
    preferred = None
    if pending and pending.get('kind') == 'rest_prosecution' and pending.get('objects'):
        preferred = pending['objects'][0]
    echoes = [echo for echo in battle.mechanics.get('hiro_echoes', [])
              if battle.fighter_for_key(echo['object']).hp > 0]
    if echoes:
        preferred = min(echoes, key=lambda item: item['due'])['object']
    for player in players:
        choose_normal(battle, player, preferred)


def simulate(witch_id, enrage, seed):
    battle = manual_battle_from_participants(PARTY, witch_id, enrage, seed)
    while not battle.result:
        plan_round(battle)
        for user_id in list(battle.choices):
            battle.confirm(user_id)
        battle.resolve()
    survivors = len(battle.living(0))
    return battle.result, battle.round, survivors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=500)
    args = parser.parse_args()
    for witch in ('ema', 'hiro'):
        print(f'[{witch}]')
        for enrage in ENRAGES:
            rows = [simulate(witch, enrage, seed) for seed in range(args.runs)]
            results = Counter(row[0] for row in rows)
            wins = [row for row in rows if row[0] == '勝利']
            rate = results['勝利'] * 100 / args.runs
            rounds = statistics.mean(row[1] for row in wins) if wins else 0
            survivors = statistics.mean(row[2] for row in wins) if wins else 0
            print(f'{enrage:>4}%  win={rate:5.1f}%  rounds={rounds:5.2f}  '
                  f'survivors={survivors:4.2f}  results={dict(results)}')


if __name__ == '__main__':
    main()
