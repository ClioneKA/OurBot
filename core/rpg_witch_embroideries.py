"""Witch reward embroidery effects shared by all battles."""
import json

REQUIREMENTS = {
    'witch_flower': 'ema', 'witch_dawn': 'hiro', 'witch_wish': 'anan',
    'witch_canvas': 'noah', 'witch_star': 'reia', 'witch_exchange': 'milia',
    'witch_echo': 'margo', 'witch_afterimage': 'nanoka', 'witch_embers': 'arisa',
    'witch_fist': 'sherry', 'witch_feather': 'hanna', 'witch_camera': 'coco',
    'witch_wings': 'meruru',
}
DESCRIPTIONS = {
    'witch_flower': '對 HP 低於 30% 的敵人，直接傷害 +5%。',
    'witch_wish': '成功對敵人施加減益後，下一次直接攻擊傷害 +4%；不疊層。',
    'witch_canvas': '對召喚物、畫作與可破壞機制物件，直接傷害 +6%。',
    'witch_star': '自己具有挑釁效果時，受到的直接傷害 -4%。',
    'witch_afterimage': '同一敵人連續兩回合直接傷害自己時，第二回合該敵人的直接傷害 -4%。',
    'witch_embers': '對具有火傷或中毒的敵人，直接傷害 +4%；兩者並存不重複加成。',
    'witch_fist': '基礎冷卻至少三回合的單體傷害技能，傷害 +4%。',
    'witch_feather': '受到的全體攻擊傷害 -4%；不減少單體或持續傷害。',
    'witch_camera': '連續行動攻擊同一目標，第二次起命中率 +3 個百分點；換目標或非攻擊行動重置。',
    'witch_dawn': '每場一次，HP 高於 50% 時受到致命攻擊，以 1 HP 存活。',
    'witch_exchange': '每場一次，自身 HP 高於 50% 時，消耗最大 HP 的 10%，救隊友至 1 HP。',
    'witch_echo': '依上一位隊友的傷害／治療技能，本次行動對應技能效果 +4%。',
    'witch_wings': '對治療前 HP 低於 50% 的目標，技能治療量 +5%。',
}


def ensure_unlock_table(db):
    db.execute('''CREATE TABLE IF NOT EXISTS rpg_witch_unlocks (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, witch_id TEXT NOT NULL,
        room_id TEXT NOT NULL, PRIMARY KEY(guild_id,user_id,witch_id))''')


def record_victory(db, guild, users, witch_ids, room_id):
    valid = set(REQUIREMENTS.values())
    db.executemany('INSERT OR IGNORE INTO rpg_witch_unlocks VALUES (?,?,?,?)',
                   [(guild, uid, key, room_id) for uid in users for key in witch_ids if key in valid])


def backfill_victories(db):
    ensure_unlock_table(db)
    db.execute('CREATE TABLE IF NOT EXISTS rpg_witch_unlock_migrations (version INTEGER PRIMARY KEY)')
    if db.execute('SELECT 1 FROM rpg_witch_unlock_migrations WHERE version=1').fetchone():
        return
    for raw, in db.execute("SELECT data FROM rpg_total_raids WHERE status='completed'").fetchall():
        room = json.loads(raw)
        battle = room.get('battle') or {}
        if battle.get('mode') == 'witch_raid' and battle.get('result') == '勝利':
            record_victory(db, room['guild_id'], room['members'], room.get('witch_ids', []), room['id'])
    db.execute('INSERT INTO rpg_witch_unlock_migrations VALUES (1)')


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
    target = context['target']
    if actor.status_stacks.get('embroidery_witch_camera'):
        target_key = fighter_key(battle, target) if target and target.team != actor.team and context['damaging'] else None
        if skill and skill.effect in ('area', 'cleave'):
            target_key = None
        context['witch_camera_target'] = target_key
        context['witch_camera_bonus'] = target_key is not None and actor.passive_state.get('witch_camera_target') == target_key
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
    if actor.status_stacks.get('embroidery_witch_camera'):
        actor.passive_state['witch_camera_target'] = context.get('witch_camera_target')
    if actor.team == 0:
        battle.mechanics.setdefault('embroidery_last_action', {})[str(actor.team)] = dict(
            user_id=actor.user_id, damage=bool(skill and skill.effect in damage_effects),
            healing=bool(skill and skill.effect in healing_effects))


def fighter_key(battle, fighter):
    return str(next(index for index, item in enumerate(battle.fighters) if item is fighter))


def debuff_applied(battle, target, source=None):
    context = battle._passive_action
    source = source or (context['actor'] if context else None)
    if source and source.team != target.team and source.status_stacks.get('embroidery_witch_wish'):
        source.passive_state['witch_wish_ready'] = True


def direct_modifiers(battle, actor, target, context, scope):
    """Called once per direct attack, never for poison or other damage-over-time."""
    multiplier = 1.0
    if actor.team != target.team:
        if actor.status_stacks.get('embroidery_witch_flower') and target.hp * 10 < target.stats['HP'] * 3:
            multiplier *= 1.05
        if actor.status_stacks.get('embroidery_witch_canvas') and target.status_stacks.get('mechanism_object'):
            multiplier *= 1.06
        if actor.status_stacks.get('embroidery_witch_embers') and (target.has('burn', battle.round) or target.has('poison', battle.round)):
            multiplier *= 1.04
    if actor.status_stacks.get('embroidery_witch_wish') and actor.passive_state.pop('witch_wish_ready', False):
        multiplier *= 1.04
    skill = context['skill'] if context else None
    if (actor.status_stacks.get('embroidery_witch_fist') and skill and context['damaging']
            and scope == 'single' and skill.effect not in ('area', 'cleave', 'holy_light') and skill.cooldown >= 3):
        multiplier *= 1.04
    if target.status_stacks.get('embroidery_witch_star') and target.has('taunt', battle.round):
        multiplier *= .96
    if target.status_stacks.get('embroidery_witch_feather') and scope == 'group':
        multiplier *= .96
    if target.status_stacks.get('embroidery_witch_afterimage') and actor.team != target.team:
        last, consecutive = target.passive_state.get('witch_afterimage_hits', {}).get(fighter_key(battle, actor), (-2, False))
        if last == battle.round - 1 or last == battle.round and consecutive:
            multiplier *= .96
    accuracy = 3 if context and context.get('witch_camera_bonus') and context['target'] is target else 0
    return multiplier, accuracy


def record_direct_hit(battle, actor, target, actual):
    if actual and target.status_stacks.get('embroidery_witch_afterimage') and actor.team != target.team:
        history = target.passive_state.setdefault('witch_afterimage_hits', {})
        key = fighter_key(battle, actor)
        last, consecutive = history.get(key, (-2, False))
        history[key] = (battle.round, last == battle.round - 1 or last == battle.round and consecutive)
