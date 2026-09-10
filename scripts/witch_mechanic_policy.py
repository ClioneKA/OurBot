"""A fixed, public-information policy for a coordinated but non-oracle party.

No RNG inspection, future-state rollouts or per-seed loadout optimization.
All choices go through the production submit/confirm API.
"""
from collections import Counter

from core.rpg_total_battle import ACTION_ATTACK, ACTION_SKILL
from core.rpg_witch_battle import ACTION_DEFEND

MECHANIC_SKILLS = {'裝甲步兵': (5, 1, 2), '騎士': (2, 4, 3),
                   '弓兵': (4, 1, 2), '僧侶': (4, 5, 3)}


def choose_round(battle, decisions):
    turn = battle.planning_round
    reserved_interrupts = set()
    assigned_damage = Counter()
    players = battle.living(0)
    # Fast actors coordinate their interrupts before slower teammates decide.
    for actor in sorted(players, key=lambda p: -p.speed):
        uid = actor.user_id
        available = [a for a in battle.available_actions(uid)
                     if a['action'] == ACTION_SKILL and not a.get('cooldown_remaining', 0)]
        skills = {battle._skill(actor, a['skill_slot'])[1].effect: a['skill_slot'] for a in available}

        def choose(action, target=None, slot=None, reason='damage'):
            targets = battle.valid_targets(uid, action, slot)
            key = battle.key(target) if target is not None else None
            if targets and key not in targets:
                return False
            battle.submit(uid, action, key if targets else None, slot)
            battle.confirm(uid)
            decisions[reason] += 1
            return True

        def skill(effect, target=None, reason='damage'):
            return effect in skills and choose(ACTION_SKILL, target, skills[effect], reason)

        if battle.forced(actor, turn):
            targets = [battle.fighter_for_key(k) for k in battle.valid_targets(uid, ACTION_ATTACK)]
            target = max(targets, key=lambda p: p.hp + p.stats['防禦']) if targets else None
            choose(ACTION_ATTACK, target, reason='forced_friendly')
            continue
        if battle.washed(actor, turn):
            choose(ACTION_DEFEND, reason='avoid_brainwash')
            continue

        # Interrupt exposed spells/links, prioritizing control, healing and heavy damage.
        exposed = set(battle.pending)
        if battle.active_link:
            exposed.update(battle.active_link['pair'])
        priorities = {'anan': 0, 'meruru': 1, 'sherry': 2, 'arisa': 3, 'ema': 4}
        candidates = sorted((w for w in battle.witches() if w.job in exposed
                             and w.job not in reserved_interrupts),
                            key=lambda w: (priorities.get(w.job, 5), w.hp))
        interrupted = False
        for witch in candidates:
            data = battle.pending.get(witch.job, battle.active_link or {})
            linked = battle.active_link and witch.job in battle.active_link['pair']
            targets = [battle.fighter_for_key(k) for k in data.get('targets', [])]
            targets = [p for p in targets if p and p.hp > 0]
            dangerous = (linked or witch.job in ('anan', 'sherry')
                or witch.job == 'ema' and any(battle.factor_stacks(p, turn) >= 2 for p in targets)
                or witch.job == 'meruru' and any(w.hp < w.stats['HP'] * .8 for w in battle.witches())
                or witch.job in ('arisa', 'coco') and data.get('phase') == 2)
            if not dangerous:
                continue
            leader = battle.witch(battle.active_link['pair'][0]) if (
                battle.active_link and witch.job in battle.active_link['pair']) else witch
            if data.get('due', turn) <= turn and actor.speed <= leader.speed:
                continue
            effect = 'hindering_shot' if actor.job == '弓兵' else 'shield_bash'
            if skill(effect, witch, 'interrupt'):
                reserved_interrupts.add(witch.job)
                if battle.active_link and witch.job in battle.active_link['pair']:
                    reserved_interrupts.update(battle.active_link['pair'])
                interrupted = True
                break
        if interrupted:
            continue

        injured = sorted(players, key=lambda p: p.hp / p.stats['HP'])
        weakest = injured[0]
        if actor.job == '僧侶':
            if weakest.hp < weakest.stats['HP'] * .35 and skill('heal', weakest, 'urgent_heal'):
                continue
            if sum(p.hp < p.stats['HP'] * .8 for p in players) >= 2 and skill('group_heal', reason='group_heal'):
                continue
            if weakest.hp < weakest.stats['HP'] * .65 and skill('holy_light', weakest, 'holy_light'):
                continue
            cleansable = sorted(players, key=lambda p: (
                battle.factor_stacks(p, turn) * 3 + 2 * p.has('burn', turn)
                + 2 * p.has('break', turn) + p.has('doubt', turn)
                + p.has('exchange', turn)), reverse=True)
            clean = cleansable[0]
            if (battle.factor_stacks(clean, turn) >= 2 or any(clean.has(k, turn)
                    for k in ('break', 'exchange')) or clean.has('burn', turn) and 'arisa' in battle.pending
                    ) and skill('cleanse', clean, 'cleanse'):
                continue
            if sum(p.hp < p.stats['HP'] * .8 for p in players) >= 2 and skill('group_heal', reason='group_heal'):
                continue
            if weakest.hp < weakest.stats['HP'] * .8 and skill('heal', weakest, 'heal'):
                continue
        if actor.job == '騎士':
            if skill('guard', reason='guard'):
                continue
            if actor.hp > actor.stats['HP'] * .45 and skill('taunt', reason='taunt'):
                continue

        # Defend against announced hits when badly injured, or against doubt.
        threatened = False
        for key, data in battle.pending.items():
            if key in reserved_interrupts or data['due'] > turn:
                continue
            targeted = battle.key(actor) in data.get('targets', [])
            group = key == 'hanna' or (data['phase'] == 2 and key in ('sherry', 'arisa', 'coco'))
            if (targeted or group) and actor.hp < actor.stats['HP'] * .45:
                threatened = True
        if threatened:
            choose(ACTION_DEFEND, reason='telegraph_defend')
            continue
        if actor.job == '僧侶' and skill('holy_light', weakest, 'holy_light'):
            continue

        # Finish a vulnerable witch instead of tunneling a full-health support.
        # Prefer removing control/healing when remaining HP is comparable.
        focus_weight = {'anan': .8, 'meruru': .9, 'hiro': 1.3}
        focus = min(battle.witches(), key=lambda w: w.hp * focus_weight.get(w.job, 1))
        objects = []
        for obj in battle.living(1):
            if obj.job in battle.ids or assigned_damage[battle.key(obj)] >= obj.hp:
                continue
            spec = battle.objects.get(battle.key(obj), {})
            owner = battle.witch(spec.get('owner'))
            if owner and owner.job in reserved_interrupts and obj.job not in ('rabbit', 'snake', 'bird'):
                continue
            # Faster owners consume paintings/rocks before this actor can break them.
            if obj.job in ('painting', 'rock', 'boulder', 'animal_painting') and owner and actor.speed <= owner.speed:
                continue
            if battle.key(obj) not in battle.valid_targets(uid, ACTION_ATTACK):
                continue
            objects.append(obj)
        if objects:
            order = {'animal_painting': 0, 'boulder': 1, 'rock': 2, 'painting': 3, 'snake': 4}
            focus = min(objects, key=lambda o: (order.get(o.job, 5), o.hp))

        offensive = ('crush', 'triple', 'double', 'strike', 'break', 'knight_charge')
        selected = None
        for effect in offensive:
            if effect not in skills:
                continue
            signature = battle._skill(actor, skills[effect])[1].name
            if actor.has('vision', turn) and battle.history.get(uid) == signature:
                continue
            selected = effect
            break
        reason = 'break_object' if focus.job not in battle.ids else 'focus_damage'
        if selected and skill(selected, focus, reason):
            power = {'crush': 2.2, 'triple': 2.55, 'double': 1.8, 'strike': 1.6}.get(selected, 1)
        elif actor.has('vision', turn) and battle.history.get(uid) == 'basic':
            choose(ACTION_DEFEND, reason='avoid_prediction_repeat')
            continue
        else:
            choose(ACTION_ATTACK, focus, reason=reason)
            power = 1
        assigned_damage[battle.key(focus)] += max(1, actor.stats['攻擊'] * power * .8 - focus.stats['防禦'] * .35)
