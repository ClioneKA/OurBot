"""Witch reward embroidery effects shared by all battles."""
DESCRIPTIONS = {
    'witch_dawn': '每場一次，HP 高於 50% 時受到致命攻擊，以 1 HP 存活。',
    'witch_exchange': '每場一次，自身 HP 高於 50% 時，消耗最大 HP 的 10%，救隊友至 1 HP。',
    'witch_echo': '依上一位隊友的傷害／治療技能，本次行動對應技能效果 +4%。',
    'witch_wings': '對治療前 HP 低於 50% 的目標，技能治療量 +5%。',
}


def survive(battle, victim, actual, direct):
    before = victim.hp
    if actual < before or before <= 0:
        return actual
    if (direct and victim.status_stacks.get('embroidery_witch_dawn') and before * 2 > victim.stats['HP']
            and not victim.status_stacks.get('witch_dawn_used')):
        victim.status_stacks['witch_dawn_used'] = 1
        battle.log.append(f'{victim.name} 的【輪迴的黎明】保留 1 HP。')
        return before - 1
    for ally in battle.living(victim.team):
        if (ally is victim or not ally.status_stacks.get('embroidery_witch_exchange')
                or ally.status_stacks.get('witch_exchange_used') or ally.hp * 2 <= ally.stats['HP']):
            continue
        ally.status_stacks['witch_exchange_used'] = 1
        cost = max(1, ally.stats['HP'] // 10)
        ally.hp = max(1, ally.hp - cost)
        battle.log.append(f'{ally.name} 的【交換的溫柔】消耗 {cost} HP，讓 {victim.name} 保留 1 HP。')
        return before - 1
    return actual


def begin(battle, context, damage_effects, healing_effects):
    actor, skill = context['actor'], context['skill']
    if not skill or not actor.status_stacks.get('embroidery_witch_echo'):
        return
    previous = battle.mechanics.get('embroidery_last_action', {}).get(str(actor.team))
    if not previous or previous['user_id'] == actor.user_id:
        return
    if previous['damage'] and skill.effect in damage_effects:
        context['multiplier'] *= 1.04
    if previous['healing'] and skill.effect in healing_effects:
        context['healing_multiplier'] = context.get('healing_multiplier', 1) * 1.04


def finish(battle, context, damage_effects, healing_effects):
    actor, skill = context['actor'], context['skill']
    if actor.team == 0:
        battle.mechanics.setdefault('embroidery_last_action', {})[str(actor.team)] = dict(
            user_id=actor.user_id, damage=bool(skill and skill.effect in damage_effects),
            healing=bool(skill and skill.effect in healing_effects))
