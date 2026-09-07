"""Simulate painting-witch total raids with a canonical Lv.50 shop-geared party."""
import argparse
from statistics import mean

from core.rpg_battle import Fighter, Rule, SKILLS
from core.rpg_character import GROWTH, ITEMS, combat_from_stats, speed_from_equipment
from core.rpg_total_battle import ACTION_ATTACK, ACTION_CANVAS, ACTION_SKILL, noah_total_battle


PARTY = ('裝甲步兵', '裝甲步兵', '騎士', '弓兵', '弓兵', '僧侶')
LOADOUTS = {
    '裝甲步兵': (2, 5, 1),
    '騎士': (1, 2, 5),
    '弓兵': (4, 1, 2),
    '僧侶': (3, 4, 1),
}
CANVAS_PRIORITY = ('騎士', '僧侶', '裝甲步兵', '弓兵')
CONTRASTS = {'white': 7, 'red': 6, 'yellow': 5, 'blue': 3,
             'orange': 4, 'green': 1, 'purple': 2, 'black': 0}


def shop_fighter(job, user_id):
    level, stage = 50, 2
    growth = GROWTH[job]
    total = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                  + stage * weight * 2 for weight in growth)
    equipped = {'武器': f'{job}:{stage}:武器', '套裝': f'{job}:{stage}:套裝'}
    combat = combat_from_stats(total, job)
    for index, stat in enumerate(('HP', '攻擊', '防禦', '治療量')):
        combat[stat] += sum(ITEMS[key].combat[index] for key in equipped.values())
    weapon = ITEMS[equipped['武器']]
    combat['命中率'] += weapon.accuracy
    rules = [Rule(slot, slot, True, 'always', 'lowest', skill_id=skill_id)
             for slot, skill_id in enumerate(LOADOUTS[job], 1)]
    potion_stat = {'裝甲步兵': '攻擊', '騎士': '防禦', '弓兵': '攻擊', '僧侶': '治療量'}[job]
    combat[potion_stat] = combat[potion_stat] * 108 // 100
    fighter = Fighter(job, 0, job, combat, speed_from_equipment(job, equipped), rules,
                      stability=weapon.stability, armed=True, user_id=user_id)
    fighter.food_name = '熾月魔魚鍋'
    fighter.food_heal_permille = 250
    fighter.food_regen_permille = 75
    fighter.food_regen_rounds = 2
    return fighter


def usable(battle, actor, name):
    for rule in actor.rules:
        skill = SKILLS[actor.job][(rule.skill_id or rule.slot) - 1]
        if skill.name == name and battle.planning_round >= actor.ready.get(rule.slot, 0):
            return rule
    return None


def prepare_black(battle):
    intent = battle.intent()
    receiver = battle.fighter_for_key(intent.target) if intent else None
    color = battle.mechanics.get('noah_intent_color')
    if receiver is None or color not in ('red', 'yellow', 'blue'):
        return
    future = battle.paint_mask(receiver) | {'red': 1, 'yellow': 2, 'blue': 4}[color]
    givers = []
    for fighter in battle.living(0):
        if fighter is receiver or not battle.paint_mask(fighter):
            continue
        future |= battle.paint_mask(fighter)
        givers.append(fighter)
    if future == 7:
        for giver in givers:
            battle.submit_paint_gift(giver.user_id, battle.key(receiver))


def choose_round(battle):
    noah = battle.noah()
    living = battle.living(0)
    canvas = {}
    if battle.noah_phase() == 1:
        needed = CONTRASTS[battle.mechanics['noah_intent_color']]
        volunteers = sorted(living, key=lambda fighter: CANVAS_PRIORITY.index(fighter.job))
        for bit, color in ((1, 'red'), (2, 'yellow'), (4, 'blue')):
            if needed & bit:
                canvas[volunteers.pop(0).user_id] = color
    elif battle.noah_phase() == 2:
        prepare_black(battle)

    eroded = next((fighter for fighter in living if fighter.status_stacks.get('source_erosion')), None)
    for actor in living:
        if actor.user_id in canvas:
            battle.submit(actor.user_id, ACTION_CANVAS, canvas[actor.user_id])
            continue
        if actor.job == '僧侶':
            rule = usable(battle, actor, '淨化') if eroded is not None else None
            target = eroded
            injured = [fighter for fighter in living if fighter.hp < fighter.stats['HP']]
            if rule is None and len([fighter for fighter in injured
                                     if fighter.hp * 100 < fighter.stats['HP'] * 70]) >= 2:
                rule, target = usable(battle, actor, '群體治療'), None
            if rule is None and injured:
                weakest = min(injured, key=lambda fighter: fighter.hp / fighter.stats['HP'])
                if weakest.hp * 100 < weakest.stats['HP'] * 55:
                    rule, target = usable(battle, actor, '治療'), weakest
            if rule is not None:
                battle.submit(actor.user_id, ACTION_SKILL,
                              battle.key(target) if target is not None else None, rule.slot)
                continue
        if actor.job == '騎士' and eroded is not None:
            rule = usable(battle, actor, '挑釁反擊')
            if rule is not None:
                battle.submit(actor.user_id, ACTION_SKILL, None, rule.slot)
                continue
        preferred = {
            '裝甲步兵': ('重裝猛擊', '重擊', '破甲'),
            '騎士': (),
            '弓兵': ('三連矢', '連射', '妨害射擊'),
            '僧侶': (),
        }[actor.job]
        rule = next((usable(battle, actor, name) for name in preferred
                     if usable(battle, actor, name) is not None), None)
        if rule is None:
            battle.submit(actor.user_id, ACTION_ATTACK, battle.key(noah))
        else:
            battle.submit(actor.user_id, ACTION_SKILL, battle.key(noah), rule.slot)


def simulate(seed, hp_per_player=14_000, attack=550, defense=340):
    battle = noah_total_battle(
        [shop_fighter(job, index) for index, job in enumerate(PARTY, 1)], seed=seed,
        hp_per_player=hp_per_player, attack=attack, defense=defense)
    while not battle.result:
        choose_round(battle)
        battle.resolve()
    return battle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=1000)
    parser.add_argument('--hp-per-player', type=int, default=14_000)
    parser.add_argument('--attack', type=int, default=550)
    parser.add_argument('--defense', type=int, default=340)
    args = parser.parse_args()
    battles = [simulate(seed, args.hp_per_player, args.attack, args.defense)
               for seed in range(args.seeds)]
    victories = [battle for battle in battles if battle.result == '勝利']
    print(f'Lv.50 商店 T50＋最高階料理與職能藥水標準六人隊｜{args.seeds} 場')
    print(f'諾亞：每人 HP {args.hp_per_player:,}／攻擊 {args.attack}／防禦 {args.defense}')
    print(f'勝率：{len(victories) / len(battles):.1%}')
    print(f'平均結束回合：{mean(battle.round for battle in battles):.2f}')
    if victories:
        print(f'勝場平均回合：{mean(battle.round for battle in victories):.2f}')
    print(f'30 回合上限率：{sum("回合上限" in battle.result for battle in battles) / len(battles):.1%}')


if __name__ == '__main__':
    main()
