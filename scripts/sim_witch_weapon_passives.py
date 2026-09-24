"""Controlled damage comparison for the unimplemented witch weapon proposals.

Run from the repository root with ``python scripts/sim_witch_weapon_passives.py``.
This script changes no game rules or saved data.
"""
from collections import defaultdict
from statistics import mean
import sys

from core.rpg_battle import Battle, Fighter, Rule, SKILLS
from core.rpg_character import COMBAT_NAMES, CRITICAL_DAMAGE_PERCENT, GROWTH, ITEMS, combat_from_stats
from core.rpg_witch_rest import WitchRestStore, equipment_id


ROUNDS = 12
SEEDS = range(100)
SCENARIOS = {
    '高血量': (0.8,) * ROUNDS,
    '收尾': (0.3,) * ROUNDS,
    '前高後低': (0.8,) * 8 + (0.3,) * 4,
}
SKILL_IDS = {'裝甲步兵': (1, 2), '弓兵': (1, 2)}
CRYSTAL_PROFILES = {
    '無結晶': {'outline_attack': 0, 'color_high': 0, 'color_low': 0,
             'source_infantry': (0, 0), 'source_archer': (0, 0)},
    '精製滿結晶': {'outline_attack': 46, 'color_high': 9, 'color_low': 12,
                 'source_infantry': (9, 6), 'source_archer': (5, 5)},
    '傑作滿結晶': {'outline_attack': 60, 'color_high': 12, 'color_low': 15,
                 'source_infantry': (12, 8), 'source_archer': (7, 7)},
}
# Reverse-fitted to the two simulated jobs and a fully socketed masterpiece T70.
# These large, job-specific values are diagnostic, not release recommendations.
TUNED_BASE_DAMAGE = {
    'ema': {'裝甲步兵': {80: 45, 85: 60}, '弓兵': {80: 75, 85: 90}},
    'hiro': {'裝甲步兵': {80: 50, 85: 80}, '弓兵': {80: 85, 85: 125}},
}
# Calibrated against complete 99% Witch Rest auto battles.
RAID_BASE_DAMAGE = {
    'ema': {'裝甲步兵': {80: 34, 85: 48}, '弓兵': {80: 56, 85: 72}},
    'hiro': {'裝甲步兵': {80: 38, 85: 64}, '弓兵': {80: 64, 85: 100}},
}
# Output-only proposal for a fixed 12-round rotation; not game balance data.
OUTPUT_BASE_DAMAGE = {
    'ema': {'裝甲步兵': {80: 45, 85: 60}, '弓兵': {80: 75, 85: 92}},
    'hiro': {'裝甲步兵': {80: 36, 85: 68}, '弓兵': {80: 61, 85: 104}},
}
OUTPUT_EMA_T80_EXECUTE = 14
OUTPUT_HIRO_LOW_HP = {'裝甲步兵': 12, '弓兵': 16}


def make_player(job, witch, tier, variant, profile):
    level = 85
    stage = 2  # The Lv.90 profession promotion has not happened yet.
    base = tuple(28 + (level - 10) * weight + stage * weight * 2
                 for weight in GROWTH[job])
    stats = combat_from_stats(base, job)
    if tier == 70:
        slug = {'裝甲步兵': 'infantry', '弓兵': 'archer'}[job]
        weapon = ITEMS[f'maze:{slug}:weapon']
        suit = ITEMS[f'maze:{slug}:suit']
    else:
        weapon = ITEMS[equipment_id(witch, job, 'weapon', tier)]
        suit = ITEMS[equipment_id(witch, job, 'suit', tier)]
    for index, name in enumerate(COMBAT_NAMES):
        stats[name] += weapon.combat[index] + suit.combat[index]
    stats['命中率'] += weapon.accuracy
    if tier == 70:
        stats['攻擊'] += profile['outline_attack']
    else:
        # Both pieces carry a grade-IV assault prefix.
        stats['攻擊'] += sum(WitchRestStore._affix_value(
            equipment_id(witch, job, slot, tier), job, 0, 'assault', 4)
            for slot in ('weapon', 'suit'))
    rules = [Rule(slot=i + 1, priority=i + 1, enabled=True, condition='always',
                  target='boss', skill_id=skill_id)
             for i, skill_id in enumerate(SKILL_IDS[job])]
    fighter = Fighter('玩家', 0, job, stats, 60, rules,
                      stability=weapon.stability,
                      critical_damage_percent=CRITICAL_DAMAGE_PERCENT[job]
                      + (8 if tier != 70 else 0))
    if tier == 70:
        first, second = profile['source_infantry' if job == '裝甲步兵' else 'source_archer']
        if job == '裝甲步兵':
            fighter.status_stacks.update(crystal_heavy_suppression=first,
                                         crystal_formation_breaker=second)
        else:
            fighter.status_stacks.update(crystal_endless_arrow=first,
                                         crystal_focused_shot=second)
        fighter.status_stacks.update(
            crystal_high_hp_damage_percent=profile['color_high'],
            crystal_low_enemy_damage_percent=profile['color_low'],
        )
    else:
        fighter.status_stacks[f'witch_{witch}_weapon'] = 1
    return fighter


def run(job, witch, tier, variant, scenario, seed, profile):
    player = make_player(job, witch, tier, variant, profile)
    boss = Fighter('假想首領', 1, '假想首領', {
        'HP': 1_000_000, '攻擊': 0, '防禦': 500, '治療量': 0,
        '命中率': 0, '閃避率': 5, '暴擊率': 0,
    }, 10, [], is_boss=True)
    battle = Battle([player, boss], seed=seed)

    if variant != '現行' and tier != 70:
        begin = battle._begin_passive_action

        def begin_with_proposal(actor, skill=None, target=None, basic=False):
            context = begin(actor, skill, target, basic)
            if context['damaging']:
                if variant == '調整版':
                    context['multiplier'] *= 1 + TUNED_BASE_DAMAGE[witch][job][tier] / 100
                elif variant == '實戰候選':
                    context['multiplier'] *= 1 + RAID_BASE_DAMAGE[witch][job][tier] / 100
                elif witch == 'ema':
                    context['multiplier'] *= 1.08 if tier == 80 else 1.12
                    if target is not None and id(target) in context.get('witch_ema_weapon_targets', ()):
                        context['multiplier'] *= (1.24 if tier == 80 else 1.28) / 1.24
                if witch == 'hiro' and skill is not None and actor.passive_state.pop('proposal_hiro_ready', False):
                    context['multiplier'] *= 1.12 if tier == 80 else 1.18
            return context

        battle._begin_passive_action = begin_with_proposal
        if witch == 'hiro':
            resolve = battle._resolve_skill

            def resolve_with_proposal(actor, rule, skill, target):
                before = actor.passive_state.get('witch_hiro_weapon_procs', 0)
                result = resolve(actor, rule, skill, target)
                if actor.passive_state.get('witch_hiro_weapon_procs', 0) > before:
                    actor.passive_state['proposal_hiro_ready'] = True
                return result

            battle._resolve_skill = resolve_with_proposal

    for turn, hp_fraction in enumerate(scenario, 1):
        battle.round = turn
        boss.hp = int(boss.stats['HP'] * hp_fraction)
        if turn % 2:
            index = ((turn - 1) // 2) % 2
            rule = player.rules[index]
            battle.use_skill(player, rule, SKILLS[job][rule.skill_id - 1], boss)
        else:
            battle.basic_attack(player, boss)
        battle.log.clear()
    return player.combat_stats['direct_damage']


def run_auto_output(job, witch, tier, scenario, seed):
    """Measure direct damage with actual automatic skill/cooldown selection."""
    player = make_player(job, witch, tier, '現行', CRYSTAL_PROFILES['傑作滿結晶'])
    player.user_id = 1
    boss = Fighter('假想首領', 1, '假想首領', {
        'HP': 1_000_000, '攻擊': 0, '防禦': 500, '治療量': 0,
        '命中率': 0, '閃避率': 5, '暴擊率': 0,
    }, 10, [], is_boss=True)
    battle = Battle([player, boss], seed=seed)
    act = battle.act
    battle.act = lambda actor: act(actor) if actor.team == 0 else None
    if tier != 70:
        begin = battle._begin_passive_action

        def proposed_begin(actor, skill=None, target=None, basic=False):
            context = begin(actor, skill, target, basic)
            if context['damaging']:
                context['multiplier'] *= 1 + OUTPUT_BASE_DAMAGE[witch][job][tier] / 100
                if (witch == 'ema' and tier == 80 and target is not None
                        and id(target) in context.get('witch_ema_weapon_targets', ())):
                    context['multiplier'] *= (1 + OUTPUT_EMA_T80_EXECUTE / 100) / 1.24
                if witch == 'hiro':
                    if (tier == 80 and target is not None
                            and target.hp * 100 <= target.stats['HP'] * 35):
                        context['multiplier'] *= 1 + OUTPUT_HIRO_LOW_HP[job] / 100
                    if (skill is not None
                            and actor.passive_state.pop('proposal_hiro_ready', False)):
                        context['multiplier'] *= 1.12 if tier == 80 else 1.18
            return context

        battle._begin_passive_action = proposed_begin
        if witch == 'hiro':
            resolve = battle._resolve_skill

            def proposed_resolve(actor, rule, skill, target):
                before = actor.passive_state.get('witch_hiro_weapon_procs', 0)
                result = resolve(actor, rule, skill, target)
                if actor.passive_state.get('witch_hiro_weapon_procs', 0) > before:
                    actor.passive_state['proposal_hiro_ready'] = True
                return result

            battle._resolve_skill = proposed_resolve
    for hp_fraction in scenario:
        boss.hp = int(boss.stats['HP'] * hp_fraction)
        battle.step()
        battle.log.clear()
    return player.combat_stats['direct_damage']


def main():
    if '--auto-output' in sys.argv:
        for job in SKILL_IDS:
            for witch in ('ema', 'hiro'):
                for label, scenario in SCENARIOS.items():
                    values = [round(mean(run_auto_output(job, witch, tier, scenario, seed)
                                         for seed in SEEDS)) for tier in (70, 80, 85)]
                    print(job, witch, label, *values)
        return
    print('12 回合；奇數回合交替使用兩個傷害技能，偶數回合普攻；100 個固定種子')
    print('Lv.85；T70 分無結晶、精製滿結晶、傑作滿結晶；T80/T85 為雙 IV 攻勢與 IV 暴傷')
    print('敵人防禦 500、閃避 5；只量玩家直接傷害，不含首領行動與生存收益')
    for job in SKILL_IDS:
        for witch in ('ema', 'hiro'):
            for label, scenario in SCENARIOS.items():
                results = defaultdict(list)
                for profile_name, profile in CRYSTAL_PROFILES.items():
                    results[f'T70 {profile_name}'] = [run(
                        job, witch, 70, '現行', scenario, seed, profile) for seed in SEEDS]
                for tier in (80, 85):
                    for variant in ('現行', '初版提案', '調整版', '實戰候選'):
                        key = f'T{tier}{variant}'
                        results[key] = [run(job, witch, tier, variant, scenario, seed,
                                            CRYSTAL_PROFILES['無結晶']) for seed in SEEDS]
                averages = {key: round(mean(values)) for key, values in results.items()}
                print(job, witch, label, averages)


if __name__ == '__main__':
    main()
