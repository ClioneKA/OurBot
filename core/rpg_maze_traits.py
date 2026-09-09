"""Painted Maze traits; all counters are persisted in fighter snapshots."""

VALUES = dict(edge=60, pursuit=90, armor=6, emergency=18, evasion=35,
              drain=18, shelter=4, erosion=100, ruin=32, cost=3, refund=6, gamble=15,
              aim_interval=8, gamble_taken=4)


def descriptions():
    v = VALUES
    return {
        'crimson:edge': f'交替使用不同傷害技能，第三段技能直接傷害 +{v["edge"]}%／層；重複技能重置連段',
        'crimson:precision': f'每累積四次有效直接命中，追加 {v["pursuit"]}%／層威力追擊；追擊不再觸發契約連鎖',
        'azure:armor': f'使用非傷害技能後獲得最大 HP {v["armor"]}%／層護盾；同類護盾不累加',
        'azure:vitality': f'每戰一次，存活時 HP 首次降至 35% 以下，獲得最大 HP {v["emergency"]}%／層護盾',
        'gold:aim': f'普攻有效命中後，最長冷卻技能縮短 1 回合／層；每 {v["aim_interval"]} 回合最多一次，不立即追加行動',
        'gold:evasion': f'每三回合最多一次，減輕下一次直接傷害 {v["evasion"]}%／層（最多 70%）',
        'verdant:renewal': f'接受有效技能治療後，下次有效直接命中吸血 {v["drain"]}%／層；最多恢復自身 10% HP，不觸發連鎖',
        'verdant:shelter': f'每三回合，各存活隊員自動治療血量比例最低的受傷隊友，回復目標最大 HP {v["shelter"]}%／層',
        'violet:focus': f'命中帶有至少兩種負面狀態的敵人，追加 {v["erosion"]}%／層威力侵蝕追擊；每兩回合最多一次',
        'violet:insight': '每四回合首次有效命中附加虛弱至下一回合結束；每多一層間隔 -1，最短兩回合，遵守免疫',
        'black:ruin': f'傷害技能消耗自身最大 HP {v["cost"]}%／層，技能直接傷害 +{v["ruin"]}%／層；擊倒敵人回復 {v["refund"]}%／層 HP，每技能最多一次；HP 不足不發動',
        'black:gamble': f'受到直接傷害 +{v["gamble_taken"]}%／層；每人每戰一次，致命直接傷害保留 1 HP，至下一回合結束直接傷害 +{v["gamble"]}%／層、受治療倍率歸零；持續傷害不觸發',
    }


def layers(actor, key):
    return actor.status_stacks.get('maze_traits', {}).get(key, 0) if actor.team == 0 else 0


def note(battle, actor, name, text):
    battle.log.append(f'{actor.name} 的【{name}】{text}')


def shield(battle, actor, amount, name):
    actor.status_stacks['maze_trait_shield'] = max(actor.status_stacks.get('maze_trait_shield', 0), amount)
    note(battle, actor, name, f'展開 {amount} 點護盾（同類護盾取較高值）。')


def begin_action(battle, context):
    actor, skill = context['actor'], context['skill']
    state = actor.passive_state
    if not skill or not context['damaging']:
        return
    count = layers(actor, 'crimson:edge')
    if count:
        previous = state.get('maze_edge_skill')
        chain = state.get('maze_edge_chain', 0) + 1 if previous != skill.effect else 1
        state.update(maze_edge_skill=skill.effect, maze_edge_chain=chain)
        if chain >= 3:
            state['maze_edge_chain'] = 0
            context['multiplier'] *= 1 + VALUES['edge'] * count / 100
            note(battle, actor, '銳筆', '第三段交替技能獲得連擊強化。')
    count = layers(actor, 'black:ruin')
    cost = max(1, actor.stats['HP'] * VALUES['cost'] * count // 100)
    if count and actor.hp > cost:
        actor.hp -= cost  # A skill cost, never fatal damage or an on-hit trigger.
        context['multiplier'] *= 1 + VALUES['ruin'] * count / 100
        context['maze_ruin_paid'] = True
        note(battle, actor, '毀形', f'消耗 {cost} HP 強化本次技能。')


def end_action(battle, context):
    actor = context['actor']
    if actor.hp <= 0:
        return
    count = layers(actor, 'azure:armor')
    if count and context['skill'] and not context['damaging']:
        shield(battle, actor, actor.stats['HP'] * VALUES['armor'] * count // 100, '厚塗')
    if context['basic'] and context['hits'] and layers(actor, 'gold:aim'):
        state = actor.passive_state
        if battle.round >= state.get('maze_aim_next', 0):
            slots = [slot for slot, ready in actor.ready.items() if ready > battle.round + 1]
            if slots:
                slot = max(slots, key=lambda slot: (actor.ready[slot], -slot))
                actor.ready[slot] = max(battle.round + 1, actor.ready[slot] - layers(actor, 'gold:aim'))
                state['maze_aim_next'] = battle.round + VALUES['aim_interval']
                note(battle, actor, '聚光', f'縮短槽 {slot} 的冷卻。')


def healed(battle, target, amount, skill_heal):
    if amount and target.hp > 0 and skill_heal and layers(target, 'verdant:renewal'):
        target.passive_state['maze_drain_ready'] = True


def before_damage(battle, target, requested, direct):
    state = target.passive_state
    if direct:
        requested *= 1 + VALUES['gamble_taken'] * layers(target, 'black:gamble') / 100
    if direct and requested > 0 and layers(target, 'gold:evasion') and battle.round >= state.get('maze_evade_next', 0):
        requested *= max(0, 100 - min(70, VALUES['evasion'] * layers(target, 'gold:evasion'))) / 100
        state['maze_evade_next'] = battle.round + 3
        note(battle, target, '掠影', '減輕本次直接傷害。')
    available = target.status_stacks.get('maze_trait_shield', 0)
    absorbed = min(available, max(0, int(requested)))
    if absorbed:
        target.status_stacks['maze_trait_shield'] -= absorbed
        requested -= absorbed
    return requested


def survive(battle, target, actual, direct):
    if (direct and actual >= target.hp > 0 and layers(target, 'black:gamble')
            and not target.passive_state.get('maze_gamble_used')):
        target.passive_state.update(maze_gamble_used=True, maze_gamble_until=battle.round + 1)
        note(battle, target, '孤注', '保留 1 HP，至下一回合結束前強化傷害，但受治療倍率歸零。')
        return target.hp - 1
    return actual


def after_damage(battle, target):
    count = layers(target, 'azure:vitality')
    if count and 0 < target.hp * 100 <= target.stats['HP'] * 35 and not target.passive_state.get('maze_emergency_used'):
        target.passive_state['maze_emergency_used'] = True
        shield(battle, target, target.stats['HP'] * VALUES['emergency'] * count // 100, '留白')


def damage_multiplier(actor, round_number):
    if actor.passive_state.get('maze_gamble_until', -1) >= round_number:
        return 1 + VALUES['gamble'] * layers(actor, 'black:gamble') / 100
    return 1


def healing_multiplier(actor, round_number):
    return 0 if actor.passive_state.get('maze_gamble_until', -1) >= round_number else 1


def after_hit(battle, actor, target, actual, context):
    if actor.team != 0 or actor.hp <= 0 or actual <= 0 or actor.passive_state.get('maze_extra_hit'):
        return
    state = actor.passive_state
    if state.pop('maze_drain_ready', False):
        amount = min(actor.stats['HP'] // 10, actual * VALUES['drain'] * layers(actor, 'verdant:renewal') // 100)
        amount = battle.restore(actor, amount * battle.healing_received_multiplier(actor))
        if amount:
            note(battle, actor, '回春', f'吸血恢復 {amount} HP。')
    if target.hp == 0 and context and context.get('maze_ruin_paid') and not context.get('maze_ruin_refunded'):
        context['maze_ruin_refunded'] = True
        amount = battle.restore(actor, actor.stats['HP'] * VALUES['refund'] * layers(actor, 'black:ruin') // 100)
        note(battle, actor, '毀形', f'擊倒敵人，回復 {amount} HP。')
    count = layers(actor, 'violet:insight')
    if count and target.hp > 0 and battle.round >= state.get('maze_insight_next', 0):
        if battle.apply_debuff(target, 'weak', battle.round + 1, actor):
            state['maze_insight_next'] = battle.round + max(2, 5 - count)
            note(battle, actor, '洞察', f'使 {target.name} 虛弱。')
    extra = []
    count = layers(actor, 'crimson:precision')
    if count:
        state['maze_pursuit_hits'] = state.get('maze_pursuit_hits', 0) + 1
        if state['maze_pursuit_hits'] >= 4:
            state['maze_pursuit_hits'] = 0
            extra.append(('點睛', VALUES['pursuit'] * count / 100))
    count = layers(actor, 'violet:focus')
    debuffs = sum(target.has(effect, battle.round) for effect in ('poison', 'weak', 'stun', 'break', 'vulnerable'))
    debuffs += sum(bool(target.status_stacks.get(effect)) for effect in ('corruption', 'drowning_mark', 'poison_arrows'))
    if count and debuffs >= 2 and battle.round >= state.get('maze_erosion_next', 0):
        state['maze_erosion_next'] = battle.round + 2
        extra.append(('蝕刻', VALUES['erosion'] * count / 100))
    state['maze_extra_hit'] = True
    try:
        for name, power in extra:
            if actor.hp > 0 and target.hp > 0:
                note(battle, actor, name, '追加一次追擊。')
                battle.hit(actor, target, power, passive_trigger=False, counterable=False)
    finally:
        state.pop('maze_extra_hit', None)


def round_end(battle):
    if battle.result or battle.round % 3:
        return
    for actor in battle.living(0):
        count = layers(actor, 'verdant:shelter')
        allies = [f for f in battle.living(0) if f.hp < f.stats['HP']]
        if count and allies:
            target = min(allies, key=lambda f: f.hp / f.stats['HP'])
            amount = battle.restore(target, target.stats['HP'] * VALUES['shelter'] * count / 100
                                    * battle.healing_received_multiplier(target))
            actor.combat_stats['healing_done'] += amount
            note(battle, actor, '庇蔭', f'使 {target.name} 恢復 {amount} HP。')
