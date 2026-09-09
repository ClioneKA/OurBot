"""Thirteen-witch encounter mechanics promoted from the validated V9 prototype.

Player creation, input validation and durable state are in rpg_witch_battle.
Historical simulation modules remain unchanged for reproducibility.
"""
from collections import Counter
from dataclasses import replace
from core.rpg_battle import Fighter, PREPARATION_TIMING
from core.rpg_total_battle import TotalRaidBattle, ACTION_ATTACK, ACTION_SKILL, ActionChoice

DEBUFFS = ('factor', 'burn', 'command', 'suggestion', 'doubt', 'watch', 'vision', 'exchange', 'no_look')
PARTNERS = {'ema': 'hiro', 'hiro': 'ema', 'sherry': 'hanna', 'hanna': 'sherry', 'noah': 'anan', 'anan': 'noah'}
ANIMALS = ('rabbit', 'snake', 'bird')
SUPPORT = ('heal', 'group_heal', 'cleanse', 'guard', 'taunt', 'bless', 'rally', 'stance')
BANS = ('ban_attack', 'ban_skill')
LINKS = (('hiro', 'ema'), ('sherry', 'hanna'), ('noah', 'anan'))


class WitchBattle(TotalRaidBattle):
    def __init__(self, fighters, ids, seed=None, max_rounds=30):
        super().__init__(fighters, seed=seed, max_rounds=max_rounds)
        self.dead_once = set()
        self.rewound = False
        self.events = Counter()
        self.link = next((pair for pair in LINKS if set(pair) <= set(ids)), None)
        order = None
        self.ids = tuple(ids)
        self.order = tuple(order or ())
        self.pending = {}
        self.next_cast = {key: 2 for key in ids}
        self.link_next = 3
        self.active_link = None
        self.reactions = set()
        self.reacted = set()
        self.stun_next = {}
        self.objects = {}
        self.history = {}
        self.current_kind = 'basic'
        self.attacking = None
        self.precise_hit = False
        self.inside_action = False
        self.recorded_hp = None
        self.animal_bag = []
        self.incoming = {}
        self.hits_by_type = {}
        self.previous_damage_type = None
        self.adaptation = None
        self.link_protected = -1
        self.ema_followup = False
        self.special_power = 1
        self.last_killer = {}

    def victim(self, actor):
        players = self.living(0)
        taunters = [p for p in players if p.has('taunt', self.round)]
        return self.rng.choice(taunters or players) if players else None

    def blast(self, actor, power):
        for player in list(self.living(0)):
            self.hit(actor, player, power, attack_scope='group')

    def witch(self, key):
        return next((f for f in self.fighters if f.team == 1 and f.job == key), None)

    def witches(self):
        return [f for f in self.living(1) if f.job in self.ids]

    def phase(self, actor):
        return min(2, len(self.dead_once - {actor.job}))

    def factor_stacks(self, actor, turn=None):
        turn = self.round if turn is None else turn
        return max(0, min(3, actor.status_stacks.get('factor', 0))) if actor.has('factor', turn) else 0

    def check_end(self):
        if self.inside_action:
            return False
        newly_dead = [f for f in self.fighters if f.job in self.ids and f.hp <= 0
                      and f.job not in self.dead_once]
        self.dead_once.update(f.job for f in newly_dead)
        for f in newly_dead:
            self.events['witch_falls'] += 1
            partner = PARTNERS.get(f.job)
            if partner in self.ids and partner not in self.reacted:
                self.reactions.add(partner)
                if partner == 'noah' and partner in self.pending:
                    self.pending[partner]['kind'] = 'black_paint'
                    self.pending[partner]['due'] += 1
                    self.reactions.discard(partner)
                    self.reacted.add(partner)
                    self.events['reactions'] += 1
            if f.job == 'hiro' and not self.rewound and self.phase(f) < 2:
                cap = f.stats['HP'] * (.5 if self.phase(f) else .3)
                f.hp = max(1, int(min(self.recorded_hp or cap, cap)))
                self.rewound = True
                self.events['rewinds'] += 1
        if self.active_link and any(self.witch(k).hp <= 0 for k in self.active_link['pair']):
            self.cancel_link()
        if not self.witches():
            self.result = '勝利' if self.living(0) else '平手'
            for f in self.living(1):
                f.hp = 0
        elif not self.living(0):
            self.result = '戰敗'
        return self.result is not None

    def clear_negative_effects(self, target):
        removed = super().clear_negative_effects(target)
        for key in DEBUFFS:
            removed += target.effects.pop(key, None) is not None
            target.status_stacks.pop(key, None)
        return removed

    def apply_debuff(self, target, effect, until, source=None):
        if effect == 'stun' and target.job in self.ids:
            # Interrupt works even when the three-round stun protection is active.
            if target.job in self.pending:
                self.cancel_spell(target.job)
                self.events['interrupts'] += 1
            if self.active_link and target.job in self.active_link['pair']:
                self.cancel_link()
                self.events['link_interrupts'] += 1
            if self.round < self.stun_next.get(target.job, 0):
                return False
            self.stun_next[target.job] = self.round + 3
        return super().apply_debuff(target, effect, until, source)

    def apply_damage(self, target, damage, share_link=True, direct=False):
        hiro = self.witch('hiro')
        if (direct and target.job == 'ema' and self.link_protected >= self.round
                and hiro and hiro.hp > 0 and not self.rewound and damage >= target.hp):
            damage = max(0, target.hp - 1)
            self.rewound = True
            self.ema_followup = True
            self.events['ema_saved'] += 1
        return super().apply_damage(target, damage, share_link, direct)

    def damage_dealt_multiplier(self, actor):
        if actor.team == 1:
            owner = self.witch(self.objects.get(self.key(actor), {}).get('owner', ''))
            return (1 + .1 * self.phase(owner or actor)) * self.special_power
        return .7 if actor.has('doubt', self.round) else 1

    def healing_done_multiplier(self, actor):
        if actor.team == 1:
            return 1 + .1 * self.phase(actor)
        return .7 if actor.has('doubt', self.round) else 1

    def damage_taken_multiplier(self, target):
        result = .5 if target.has('defend', self.round) else 1
        if target.team == 1:
            if target.job == 'hanna' and (self.round % 2 or target.has('float', self.round)) and self.attacking:
                if self.attacking.job in ('裝甲步兵', '騎士') and not self.precise_hit:
                    result *= .7
            if target.has('spotlight', self.round):
                result *= .75 if self.phase(target) else 1
            if target.has('peace', self.round):
                result *= .8
            if target.has('snake_guard', self.round):
                snakes = [f for f in self.living(1) if f.job == 'snake']
                if snakes:
                    result *= .75
            if target.job == 'milia' and self.adaptation == (self.round, self.current_kind):
                result *= .5
        return max(.4, result)

    def hit(self, actor, target, power=1.0, **kwargs):
        old_actor = self.attacking
        old_precise = self.precise_hit
        self.attacking = actor
        self.precise_hit = bool(kwargs.get('precise'))
        defense = target.stats['防禦']
        broken = target.effects.pop('break', None)
        if broken is not None and broken >= self.round:
            target.stats['防禦'] = int(defense * .75)
        before = target.hp
        try:
            result = super().hit(actor, target, power, **kwargs)
            if actor.team == 0 and before > target.hp:
                self.hits_by_type[self.current_kind] = self.hits_by_type.get(self.current_kind, 0) + before - target.hp
                if target.hp <= 0:
                    self.last_killer[target.job] = self.key(actor)
            return result
        finally:
            target.stats['防禦'] = defense
            if broken is not None:
                target.effects['break'] = broken
            self.attacking = old_actor
            self.precise_hit = old_precise

    def mark(self, target, key, duration=1, stacks=False):
        previous = self.factor_stacks(target) if key == 'factor' else target.status_stacks.get(key, 0)
        if self.apply_debuff(target, key, self.round + duration):
            if stacks:
                target.status_stacks[key] = min(3, previous + 1)
            if key == 'factor':
                self.log.append(f'{target.name} 的魔女因子增加至 {target.status_stacks.get(key, 0)}/3 層。')

    def add_object(self, owner, kind, hp_fraction, power=0, group=False):
        hp = max(1, round(owner.stats['HP'] * hp_fraction))
        obj = Fighter(kind, 1, kind, {'HP': hp, '攻擊': owner.stats['攻擊'], '防禦': 0,
            '治療量': 0, '命中率': 105, '閃避率': 0, '暴擊率': 0}, 35, [], is_boss=False)
        self.fighters.append(obj)
        self.objects[self.key(obj)] = dict(owner=owner.job, kind=kind, power=power,
            group=group, born=self.round, next=self.round + 1)
        self.events['objects_created'] += 1
        return self.key(obj)

    def cancel_spell(self, key):
        pending = self.pending.pop(key, None)
        if pending:
            for obj in pending.get('objects', []):
                self.fighter_for_key(obj).hp = 0

    def cancel_link(self):
        if self.active_link:
            for key in self.active_link.get('objects', []):
                self.fighter_for_key(key).hp = 0
            for key in self.active_link['pair']:
                self.next_cast[key] = max(self.next_cast[key], self.round + 1)
        self.active_link = None
        self.link_next = self.round + 5

    def prepare(self, actor, kind=None):
        phase = self.phase(actor)
        data = dict(due=self.round + 1, phase=phase, kind=kind or actor.job, objects=[])
        players = self.living(0)
        target = self.victim(actor)
        data['targets'] = [self.key(target)] if target else []
        if actor.job in ('anan', 'margo', 'coco', 'nanoka'):
            count = min(len(players), 2 if phase else 1)
            if actor.job == 'anan':
                count = min(count, max(1, len(players) - 1))
            if actor.job in ('nanoka', 'margo') and phase == 2:
                count = len(players)
            data['targets'] = [self.key(p) for p in self.rng.sample(players, count)]
        if actor.job == 'milia' and len(players) >= 2:
            data['targets'] = [self.key(p) for p in self.rng.sample(players, 2)]
            for key in data['targets']:
                self.mark(self.fighter_for_key(key), 'exchange', 1)
        if actor.job == 'arisa' and phase == 1:
            burned = [p for p in players if p.has('burn', self.round)]
            data['source'] = self.key(burned[0]) if burned else None
        if actor.job == 'noah':
            data['objects'].append(self.add_object(actor, 'painting', .06, 1.3))
            if phase == 2:
                data['objects'].append(self.add_object(actor, 'painting', .06, .7, True))
        if actor.job == 'hanna':
            data['objects'] = [self.add_object(actor, 'rock', .04, .4 if phase == 2 else .8, True)
                               for _ in range(3 if phase == 2 else 1)]
        if actor.job == 'reia' and phase == 2:
            half = max(1, (len(players) + 1) // 2)
            data['targets'] = [self.key(p) for p in self.rng.sample(players, half)]
        self.pending[actor.job] = data
        self.next_cast[actor.job] = self.round + (4 if phase == 2 and actor.job in ('ema', 'margo', 'arisa', 'meruru') else 3)
        self.events['telegraphs'] += 1

    def prepare_link(self):
        if not self.link or self.round < self.link_next or self.active_link:
            return
        if not all(self.witch(k).hp > 0 for k in self.link):
            return
        if any(k in self.pending or k in self.reactions for k in self.link):
            return
        leader = self.witch(self.link[0])
        if self.link == ('hiro', 'ema') and (self.rewound or self.witch('ema').hp >= self.witch('ema').stats['HP'] * .5):
            return
        if self.link == ('noah', 'anan') and len([f for f in self.living(1) if f.job in ANIMALS]) >= 2:
            return
        data = dict(pair=self.link, due=self.round + 1, prepared=self.round, objects=[])
        if self.link == ('sherry', 'hanna'):
            data['objects'] = [self.add_object(leader, 'boulder', .08)]
            target = self.victim(leader)
            data['target'] = self.key(target) if target else None
        elif self.link == ('noah', 'anan'):
            if not self.animal_bag:
                self.animal_bag = list(ANIMALS)
                self.rng.shuffle(self.animal_bag)
            data['animal'] = self.animal_bag.pop()
            data['objects'] = [self.add_object(leader, 'animal_painting', .08)]
            candidates = self.living(0)
            for p in self.rng.sample(candidates, min(2, max(0, len(candidates) - 1))):
                self.mark(p, 'no_look', 1)
        self.active_link = data
        self.events['link_prepares'] += 1

    def cast_link(self, actor):
        data = self.active_link
        if self.round < data['due'] or actor.job != data['pair'][0]:
            return
        if data['objects'] and not any(self.fighter_for_key(k).hp > 0 for k in data['objects']):
            self.events['links_broken'] += 1
            self.cancel_link()
            return
        if data['pair'] == ('hiro', 'ema'):
            self.link_protected = self.round
        elif data['pair'] == ('sherry', 'hanna'):
            self.blast(actor, 1.2)
            target = self.fighter_for_key(data.get('target'))
            if target and target.hp > 0:
                self.hit(actor, target, .8)
        else:
            animal = data['animal']
            key = self.add_object(actor, animal, {'rabbit': .12, 'snake': .15, 'bird': .10}[animal])
            summon = self.fighter_for_key(key)
            summon.stats['攻擊'] = round(actor.stats['攻擊'] * {'rabbit': .6, 'snake': 0, 'bird': .7}[animal])
            self.events['summons'] += 1
        self.events['link_casts'] += 1
        # Followers have spent the turn even after this object is cleared.
        self.cancel_link()

    def spell(self, actor, data):
        phase, kind = data['phase'], data['kind']
        targets = [self.fighter_for_key(k) for k in data['targets']]
        targets = [p for p in targets if p and p.hp > 0]
        target = targets[0] if targets else None
        independent = (actor.job in ('hanna', 'reia', 'noah') or actor.job == 'milia' and phase == 2
                       or actor.job in ('arisa', 'coco', 'sherry') and phase == 2)
        if not target and not independent:
            self.log.append(f'{actor.name} 沒有有效預告目標，本次指定魔法取消，不轉移目標。')
            return
        self.events['special_casts'] += 1
        if kind in ('hiro_reaction', 'ema_followup'):
            self.hit(actor, target, 1.8 if kind == 'hiro_reaction' else 1.6)
        elif actor.job == 'ema':
            victims = targets
            for p in list(victims):
                stacks = self.factor_stacks(p)
                if not stacks:
                    self.log.append(f'{p.name} 沒有有效魔女因子，本次引爆取消。')
                    continue
                self.log.append(f'{p.name} 的 {stacks} 層魔女因子被引爆。')
                self.hit(actor, p, (.8 + .25 * stacks) if phase == 2 else (1.3 + .3 * stacks), attack_scope='group' if phase == 2 else 'single')
                p.status_stacks.pop('factor', None)
                p.effects.pop('factor', None)
                self.log.append(f'{p.name} 的魔女因子已引爆並清空。')
        elif actor.job == 'hiro':
            for _ in range(2 if phase == 2 else 1):
                if target.hp > 0:
                    self.hit(actor, target, .9 if phase == 2 else 1.2)
        elif actor.job == 'anan':
            for p in targets:
                self.mark(p, 'command' if phase and len(self.living(0)) > 1 else 'suggestion', 1)
        elif actor.job == 'margo':
            for p in (targets if phase != 1 else targets[:1]):
                if not p.has('defend', self.round):
                    self.mark(p, 'doubt', 1)
        elif actor.job == 'nanoka':
            for p in targets:
                self.mark(p, 'vision', 1)
        elif actor.job == 'milia':
            if phase == 2:
                self.adaptation = (self.round + 1, self.previous_damage_type)
            elif len(targets) == 2 and all(p.has('exchange', self.round) for p in targets):
                a, b = targets
                ar, br = a.hp / a.stats['HP'], b.hp / b.stats['HP']
                a.hp, b.hp = max(1, int(a.stats['HP'] * br)), max(1, int(b.stats['HP'] * ar))
                if phase:
                    for key in ('burn', 'break', 'weak', 'bless'):
                        av, bv = a.effects.pop(key, None), b.effects.pop(key, None)
                        if av is not None:
                            b.effects[key] = av
                        if bv is not None:
                            a.effects[key] = bv
            elif len(self.living(0)) == 1:
                self.hit(actor, target, .9)
                self.apply_debuff(target, 'weak', self.round + 1)
        elif actor.job == 'reia':
            if phase < 2:
                actor.effects['spotlight'] = self.round + 1
            else:
                for p in targets:
                    self.hit(actor, p, 1.1, attack_scope='group')
                if kind != 'reia_return':
                    others = [self.key(p) for p in self.living(0) if p not in targets]
                    if others:
                        self.pending[actor.job] = dict(due=self.round + 1, phase=2, kind='reia_return', targets=others, objects=[])
        elif actor.job == 'sherry':
            if phase == 2:
                self.blast(actor, 1)
                if target and target.hp > 0:
                    self.hit(actor, target, 1)
            elif phase or kind == 'sherry_reaction':
                first, second = (1, 1) if kind == 'sherry_reaction' else (.7, 1.3)
                if self.hit(actor, target, first):
                    self.apply_debuff(target, 'break', self.round + 1)
                if target.hp > 0:
                    self.hit(actor, target, second)
            else:
                self.hit(actor, target, 1.8)
            if phase == 2 or kind == 'sherry_reaction':
                actor.effects['break'] = self.round + 1
        elif actor.job == 'arisa':
            if phase == 2:
                for p in list(self.living(0)):
                    self.hit(actor, p, .9 + (.5 if p.has('burn', self.round) else 0), attack_scope='group')
                    p.effects.pop('burn', None)
            else:
                source = self.fighter_for_key(data.get('source'))
                spread = bool(source and source.hp > 0 and source.has('burn', self.round))
                if self.hit(actor, target, .9):
                    self.mark(target, 'burn', 1)
                if phase and spread:
                    burned = [p for p in self.living(0) if p.has('burn', self.round)]
                    clean = [p for p in self.living(0) if p not in burned]
                    if burned and clean:
                        self.mark(self.rng.choice(clean), 'burn', 1)
        elif actor.job == 'coco':
            victims = self.living(0) if phase == 2 else targets
            for p in list(victims):
                self.hit(actor, p, (.8 + (.4 if p.has('watch', self.round) else 0)) if phase == 2 else (.9 if phase else 1.2), attack_scope='group' if phase == 2 else 'single')
                p.effects.pop('watch', None)
        elif actor.job == 'meruru':
            if not actor.has('peace', self.round):
                ally = actor if phase == 2 else min(self.witches(), key=lambda p: p.hp / p.stats['HP'])
                self.heal(actor, ally, int(ally.stats['HP'] * (.12 if phase == 2 else .08)))
                if phase:
                    self.clear_negative_effects(ally)
                if phase == 2:
                    actor.effects['peace'] = self.round + 1
        elif actor.job in ('noah', 'hanna'):
            for key in data['objects']:
                obj = self.fighter_for_key(key)
                if obj.hp <= 0:
                    self.events['objects_broken'] += 1
                    continue
                spec = self.objects[key]
                power = spec['power'] * (1.25 if kind == 'black_paint' else 1)
                if spec['group']:
                    self.blast(actor, power)
                elif target:
                    self.hit(actor, target, power)
                    if phase == 1 and target.hp > 0:
                        self.hit(actor, target, .6)
                obj.hp = 0

    def animal_act(self, actor):
        spec = self.objects[self.key(actor)]
        if self.round <= spec['born'] or self.round < spec['next']:
            return
        if actor.job == 'snake':
            if self.witches():
                ally = min(self.witches(), key=lambda f: f.hp / f.stats['HP'])
                ally.effects['snake_guard'] = self.round + 1
            spec['next'] = self.round + 2
        elif not spec.get('charging'):
            target = self.victim(actor)
            spec['target'] = self.key(target) if target else None
            spec['charging'] = True
            spec['next'] = self.round + 1
        else:
            if actor.job == 'rabbit':
                for p in list(self.living(0)):
                    if self.hit(actor, p, .7, attack_scope='group'):
                        self.mark(p, 'burn', 1)
            else:
                p = self.fighter_for_key(spec['target'])
                if p and p.hp > 0 and self.hit(actor, p, 1.3):
                    self.apply_debuff(p, 'break', self.round + 1)
            spec['charging'] = False
            spec['next'] = self.round + 2
            self.events['animal_attacks'] += 1

    def act(self, actor):
        if actor.job not in self.ids:
            if actor.job in ANIMALS:
                self.animal_act(actor)
            return
        if actor.job in self.link_spent:
            if self.active_link:
                self.cast_link(actor)
            return
        if actor.job == 'hiro' and (self.recorded_hp is None or self.round % 3 == 1):
            self.recorded_hp = actor.hp
        if actor.job in self.pending and self.pending[actor.job]['due'] <= self.round:
            data = self.pending.pop(actor.job)
            self.spell(actor, data)
            return
        if actor.job in self.reactions:
            self.reactions.remove(actor.job)
            self.reacted.add(actor.job)
            self.events['reactions'] += 1
            if actor.job == 'ema':
                for p in self.living(0):
                    self.mark(p, 'factor', 30, True)
            else:
                kind = {'hiro': 'hiro_reaction', 'sherry': 'sherry_reaction', 'noah': 'black_paint'}.get(actor.job)
                self.prepare(actor, kind)
                if actor.job == 'hanna':
                    actor.effects['float'] = self.round + 1
                if actor.job == 'anan':
                    candidates = self.living(0)
                    count = min(len(self.pending['anan']['targets']) + 1, max(1, len(candidates) - 1))
                    self.pending['anan']['targets'] = [self.key(p) for p in self.rng.sample(candidates, count)]
                if actor.job == 'hiro':
                    killer = self.fighter_for_key(self.last_killer.get('ema'))
                    if killer and killer.hp > 0:
                        self.pending['hiro']['targets'] = [self.key(killer)]
            return
        if actor.job == 'ema' and self.ema_followup:
            self.ema_followup = False
            self.prepare(actor, 'ema_followup')
            return
        if self.round >= self.next_cast[actor.job]:
            self.prepare(actor)
            return
        p = self.victim(actor)
        if p and self.hit(actor, p, .75 if actor.job in ('anan', 'meruru', 'reia', 'milia', 'margo') else 1):
            if actor.job == 'ema':
                self.mark(p, 'factor', 30, True)
            if actor.job == 'coco':
                self.mark(p, 'watch', 3)

    def _resolve_player(self, actor, choice):
        if choice.action == 'defend':
            actor.effects['defend'] = self.round
            self.history[actor.user_id] = 'defend'
            return
        skill = self._skill(actor, choice.skill_slot)[1] if choice.action == ACTION_SKILL else None
        offensive = choice.action == ACTION_ATTACK or (skill and skill.effect not in ('heal', 'group_heal', 'cleanse', 'guard', 'taunt', 'bless', 'stance', 'rally'))
        if actor.has('command', self.round) and skill and offensive:
            choice = ActionChoice(actor.user_id, ACTION_ATTACK, choice.target)
            skill = None
        target = self.fighter_for_key(choice.target)
        if target and target.job == 'animal_painting' and actor.has('no_look', self.round):
            target = self.pick_target()
            choice = replace(choice, target=self.key(target))
        focus = next((f for f in self.witches() if f.has('spotlight', self.round)), None)
        if offensive and focus and target and target.job in self.ids and not (skill and skill.effect in ('area', 'cleave', 'holy_light')):
            choice = replace(choice, target=self.key(focus))
        signature = skill.name if skill else 'basic'
        repeat = self.history.get(actor.user_id) == signature
        self.current_kind = ('area' if skill and skill.effect in ('area', 'cleave', 'holy_light') else 'skill' if skill else 'basic')
        old_attack = actor.stats['攻擊']
        if actor.has('vision', self.round) and repeat:
            actor.stats['攻擊'] = round(old_attack * .6)
        super()._resolve_player(actor, choice)
        actor.stats['攻擊'] = old_attack
        actor.effects.pop('doubt', None)
        if offensive and actor.has('suggestion', self.round):
            self.mark(actor, 'doubt', 1)
        if repeat and actor.has('vision', self.round):
            watcher = self.witch('nanoka')
            if watcher and watcher.hp > 0 and self.phase(watcher) and actor.hp > 0:
                self.hit(watcher, actor, .7)
        self.history[actor.user_id] = signature


    def pick_target(self):
        witches = self.witches()
        if self.order:
            return min(witches, key=lambda f: self.order.index(f.job))
        priority = {'meruru': 0, 'anan': 1, 'margo': 2, 'hiro': 3}
        return min(witches, key=lambda f: (priority.get(f.job, 4), f.hp))

    def resolve(self, use_defaults=False):
        if self.check_end():
            return self.result
        self.round += 1
        self.hits_by_type = {}
        self.prepare_link()
        self.link_spent = set(self.active_link['pair']) if self.active_link else set()
        choices = dict(self.choices)
        self.choices.clear()
        order = list(self.living(0)) + list(self.living(1))
        self.rng.shuffle(order)
        def priority(f):
            c = choices.get(f.user_id)
            prep = bool(c and (c.action == 'defend' or (c.action == ACTION_SKILL and self._skill(f, c.skill_slot)[1].timing == PREPARATION_TIMING)))
            return prep, f.speed
        order.sort(key=priority, reverse=True)
        for actor in order:
            if actor.hp <= 0:
                continue
            self.inside_action = True
            try:
                if self._begin_actor_turn(actor):
                    if actor.team == 0:
                        self._resolve_player(actor, choices[actor.user_id])
                    else:
                        self.current_kind = 'enemy'
                        self.act(actor)
            finally:
                self.inside_action = False
            if self.check_end():
                break
        if not self.result:
            self.inside_action = True
            for p in list(self.living(0)):
                if p.has('burn', self.round):
                    self.apply_damage(p, max(1, p.stats['HP'] * 3 // 100))
                    self.maybe_eat(p)
                if p.hp > 0 and p.food_regen_left and self.round >= p.food_regen_start:
                    self.restore(p, max(1, p.stats['HP'] * p.food_regen_permille // 1000))
                    p.food_regen_left -= 1
            self.inside_action = False
            self.check_end()
        self.previous_damage_type = max(self.hits_by_type, key=self.hits_by_type.get) if self.hits_by_type else None
        reia = self.witch('reia')
        if reia and reia.effects.get('spotlight') == self.round:
            reia.effects['break'] = self.round + 1
        if not self.result and self.round >= self.max_rounds:
            self.result = '平手（達回合上限）'
        return self.result

class WitchBattleV3(WitchBattle):
    def __init__(self, *args, pacing=True, stagger=False, tuning=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.pacing = pacing
        self.tuning = tuning
        if stagger:
            # Publicly predictable stagger; does not read player stats or random outcomes.
            for i, key in enumerate(sorted(self.ids, key=lambda k: -self.witch(k).speed)):
                self.next_cast[key] = 2 + i

    def cast_link(self, actor):
        pair = self.active_link['pair']
        previous = self.special_power
        if self.pacing and pair == ('sherry', 'hanna'):
            self.special_power *= .8
        try:
            super().cast_link(actor)
        finally:
            self.special_power = previous

    def add_object(self, owner, kind, hp_fraction, power=0, group=False):
        if self.pacing and kind in (*ANIMALS, 'animal_painting'):
            hp_fraction *= 1.25
        return super().add_object(owner, kind, hp_fraction, power, group)

    def animal_act(self, actor):
        previous = self.special_power
        if self.pacing:
            self.special_power *= 1.2
        try:
            super().animal_act(actor)
        finally:
            self.special_power = previous

    def spell(self, actor, data):
        previous = self.special_power
        if self.tuning and actor.job == 'sherry':
            self.special_power *= .65
        if self.pacing and data['kind'] in ('hiro_reaction', 'ema_followup'):
            self.special_power *= .85
        try:
            super().spell(actor, data)
        finally:
            self.special_power = previous
        if self.tuning and actor.job in ('anan', 'milia', 'margo'):
            target = self.fighter_for_key(data.get('followup_target'))
            if target and target.hp > 0:
                self.hit(actor, target, .75)
                self.events['support_followups'] += 1


class WitchBattleV4(WitchBattleV3):
    def __init__(self, *args, changed=('anan', 'meruru', 'sherry'), command_base=3,
                 command_locked=False, final_command_turns=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.changed = set(changed)
        self.command_base = command_base
        self.command_locked = command_locked
        self.final_command_turns = final_command_turns
        self.commands = {}
        self.prayer_shields = {}
        self.recovery = {}

    def prepare(self, actor, kind=None):
        super().prepare(actor, kind)
        data = self.pending[actor.job]
        if actor.job == 'anan' and 'anan' in self.changed:
            players = self.living(0)
            count = min(self.command_base + self.phase(actor) + (kind == 'anan_reaction'), max(0, len(players) - 1))
            data['targets'] = []
            for p in self.rng.sample(players, count):
                ban = self.rng.choice(BANS)
                for other in BANS:
                    p.effects.pop(other, None)
                self.mark(p, ban, self.final_command_turns if self.phase(actor) == 2 else 1)
                self.commands[self.key(p)] = (ban, data['due'])
                data['targets'].append(self.key(p))
            self.events['commands_prepared'] += count
        if actor.job == 'meruru' and 'meruru' in self.changed:
            target = min(self.witches(), key=lambda f: f.hp / f.stats['HP'])
            data['rescue_target'] = self.key(target)
            self.events['prayers_prepared'] += 1

    def cancel_spell(self, key):
        super().cancel_spell(key)
        if key == 'anan' and 'anan' in self.changed:
            for p in self.living(0):
                for ban in BANS:
                    p.effects.pop(ban, None)
            self.commands.clear()
        if key == 'meruru' and 'meruru' in self.changed:
            self.recovery[key] = self.round + 1
            self.next_cast[key] = max(self.next_cast[key], self.round + 2)

    def clear_negative_effects(self, target):
        removed = super().clear_negative_effects(target)
        if not self.command_locked:
            for ban in BANS:
                removed += target.effects.pop(ban, None) is not None
        return removed

    def check_end(self):
        result = super().check_end()
        if getattr(self, 'commands', None):
            anan = self.witch('anan')
            if anan and anan.hp <= 0:
                self.commands.clear()
                for p in self.living(0):
                    for ban in BANS:
                        p.effects.pop(ban, None)
        return result

    def apply_debuff(self, target, effect, until, source=None):
        if self.command_locked and effect in BANS:
            # Encounter instruction, independent of generic status immunity.
            target.effects[effect] = until
            return True
        result = super().apply_debuff(target, effect, until, source)
        if effect == 'stun' and target.job == 'anan' and getattr(self, 'commands', None):
            self.cancel_spell('anan')
        return result

    def spell(self, actor, data):
        if actor.job == 'anan' and 'anan' in self.changed:
            # Commands act on the announced player's action, not Anan's initiative.
            return
        if actor.job == 'meruru' and 'meruru' in self.changed:
            target = self.fighter_for_key(data['rescue_target'])
            if not target or target.hp <= 0:
                self.events['prayers_denied'] += 1
                return
            phase = data['phase']
            requested = int(target.stats['HP'] * (.18 + .02 * phase))
            effective = int(requested * self.healing_done_multiplier(actor)
                            * self.healing_received_multiplier(target))
            overflow = max(0, effective - (target.stats['HP'] - target.hp))
            self.heal(actor, target, requested)
            if phase:
                self.clear_negative_effects(target)
            shield = min(int(target.stats['HP'] * .12), int(overflow * (.5 + .15 * phase)))
            old = self.prayer_shields.get(self.key(target), (0, -1))
            if shield and (old[1] < self.round or shield > old[0]):
                self.prayer_shields[self.key(target)] = (shield, self.round + 2)
            self.events['prayers_completed'] += 1
            return
        super().spell(actor, data)
        if actor.job == 'sherry' and 'sherry' in self.changed:
            self.recovery['sherry'] = self.round + 1
            self.next_cast['sherry'] = max(self.next_cast['sherry'], self.round + 2)

    def apply_damage(self, target, damage, share_link=True, direct=False):
        shield, until = self.prayer_shields.get(self.key(target), (0, -1))
        if until >= self.round and damage > 0:
            absorbed = min(shield, damage)
            damage -= absorbed
            self.prayer_shields[self.key(target)] = (shield - absorbed, until)
            self.events['prayer_absorbed'] += absorbed
        return super().apply_damage(target, damage, share_link, direct)

    def act(self, actor):
        if actor.job in self.recovery and self.round <= self.recovery[actor.job]:
            target = self.victim(actor)
            if target:
                self.hit(actor, target, .4)
            self.events['recovery_turns'] += 1
            return
        if (actor.job == 'anan' and 'anan' in self.changed and actor.job in self.reactions
                and actor.job not in self.pending and actor.job not in self.link_spent):
            self.reactions.remove(actor.job)
            self.reacted.add(actor.job)
            self.events['reactions'] += 1
            self.prepare(actor, 'anan_reaction')
            return
        super().act(actor)

    def cast_link(self, actor):
        ready = self.active_link and self.active_link['pair'] == ('sherry', 'hanna') and self.round >= self.active_link['due'] and actor.job == 'sherry'
        before = self.events['link_casts']
        super().cast_link(actor)
        if ready and 'sherry' in self.changed and self.events['link_casts'] > before:
            self.recovery['sherry'] = self.round + 1
            self.next_cast['sherry'] = max(self.next_cast['sherry'], self.round + 2)

    def prepare_link(self):
        if self.link and any(self.round <= self.recovery.get(k, -1) for k in self.link):
            return
        super().prepare_link()

    def _violation(self, actor, choice):
        entry = self.commands.get(self.key(actor))
        if not entry or entry[1] > self.round or not actor.has(entry[0], self.round):
            return False
        if entry[0] == 'ban_skill':
            return choice.action == ACTION_SKILL
        if choice.action == ACTION_ATTACK:
            return True
        return bool(choice.action == ACTION_SKILL
                    and self._skill(actor, choice.skill_slot)[1].effect not in SUPPORT)

    def _resolve_player(self, actor, choice):
        violation = self._violation(actor, choice)
        super()._resolve_player(actor, choice)
        anan = self.witch('anan')
        if violation and self._violation(actor, choice) and anan and anan.hp > 0 and actor.hp > 0:
            self.hit(anan, actor, 1.2, precise=True)
            self.events['command_punishments'] += 1


class WitchBattleV8(WitchBattleV4):
    def __init__(self, *args, brainwash_aware=True, **kwargs):
        super().__init__(*args, command_base=1, command_locked=True, **kwargs)
        self.washed_actor = None
        self.washed_effect = None
        self.brainwash_aware = brainwash_aware

    def washed(self, actor, turn):
        entry = self.commands.get(self.key(actor))
        return bool(entry and entry[1] <= turn and actor.has('brainwash', turn))

    def prepare(self, actor, kind=None):
        if actor.job != 'anan':
            return super().prepare(actor, kind)
        WitchBattleV3.prepare(self, actor, kind)
        count = min(1 + self.phase(actor), max(0, len(self.living(0)) - 1))
        selected = self.rng.sample(self.living(0), count)
        self.pending['anan']['targets'] = [self.key(p) for p in selected]
        for p in selected:
            p.effects['brainwash'] = self.round + 1
            self.commands[self.key(p)] = ('brainwash', self.round + 1)
        self.events['brainwash_prepared'] += count

    def _violation(self, actor, choice):
        return False

    def cancel_spell(self, key):
        super().cancel_spell(key)
        if key == 'anan':
            for p in self.living(0):
                p.effects.pop('brainwash', None)

    def check_end(self):
        result = super().check_end()
        anan = self.witch('anan')
        if anan and anan.hp <= 0:
            for p in self.living(0):
                p.effects.pop('brainwash', None)
        return result

    def _fallback_target(self, actor, choice, enemy):
        if actor is self.washed_actor:
            if self.washed_effect in ('heal', 'holy_light'):
                candidates = self.witches()
            elif self.washed_effect not in SUPPORT and self.washed_effect != 'defend':
                candidates = [p for p in self.living(0) if p is not actor]
            else:
                return super()._fallback_target(actor, choice, enemy)
            target = self.fighter_for_key(choice.target)
            return target if target in candidates else self.rng.choice(candidates) if candidates else None
        return super()._fallback_target(actor, choice, enemy)

    def _resolve_player(self, actor, choice):
        old_actor, old_effect = self.washed_actor, self.washed_effect
        if self.washed(actor, self.round):
            self.washed_actor = actor
            self.washed_effect = self._skill(actor, choice.skill_slot)[1].effect if choice.action == ACTION_SKILL else choice.action
            self.events['brainwashed_actions'] += 1
        try:
            return super()._resolve_player(actor, choice)
        finally:
            self.washed_actor, self.washed_effect = old_actor, old_effect

    def _resolve_skill(self, actor, rule, skill, target):
        if actor is not self.washed_actor or skill.effect not in ('group_heal', 'rally', 'area', 'cleave', 'holy_light'):
            return super()._resolve_skill(actor, rule, skill, target)
        # Reuse hit/heal and passive action wrappers; only group enumeration changes.
        targets = self.witches() if skill.effect in ('group_heal', 'rally') else [p for p in self.living(0) if p is not actor]
        if not targets:
            self.events['brainwash_no_target'] += 1
            return
        self.record_skill(actor, skill.name)
        cooldown = max(1, skill.cooldown - actor.cooldown_reduction)
        if actor.first_skill_cooldown_reduction and not actor.first_skill_cooldown_used and skill.cooldown >= 2:
            cooldown = max(1, cooldown - actor.first_skill_cooldown_reduction)
            actor.first_skill_cooldown_used = True
        actor.ready[rule.slot] = self.round + cooldown + 1
        if skill.effect in ('group_heal', 'rally'):
            if skill.effect == 'rally':
                targets = [self.rng.choice(targets)]
            for witch in targets:
                self.heal(actor, witch, actor.stats['治療量'] * 65 // 100 if skill.effect == 'group_heal' else actor.stats['HP'] // 2)
        else:
            power = {'area': .4, 'cleave': 1.2, 'holy_light': .9}[skill.effect]
            for ally in targets:
                for _ in range(3 if skill.effect == 'area' else 1):
                    if actor.hp > 0 and ally.hp > 0:
                        self.hit(actor, ally, power, attack_scope='group')
            if skill.effect == 'holy_light' and actor.hp > 0 and self.witches():
                self.heal(actor, target if target in self.witches() else self.rng.choice(self.witches()), actor.stats['治療量'] * 70 // 100)


class WitchBattleV9(WitchBattleV8):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.forced_brainwash = {}

    def prepare(self, actor, kind=None):
        if actor.job != 'anan':
            return super().prepare(actor, kind)
        WitchBattleV3.prepare(self, actor, kind)
        phase = self.phase(actor)
        count = min((1, 2, 4)[phase], max(0, len(self.living(0)) - 1))
        selected = self.rng.sample(self.living(0), count)
        self.pending['anan']['targets'] = [self.key(p) for p in selected]
        for p in selected:
            key = self.key(p)
            p.effects['brainwash'] = self.round + 1
            self.commands[key] = ('brainwash', self.round + 1)
            self.forced_brainwash[key] = self.round + 1 if phase else -1
        self.events['brainwash_prepared'] += count
        self.events[f'brainwash_phase_{phase}_casts'] += 1

    def forced(self, actor, turn):
        return self.washed(actor, turn) and self.forced_brainwash.get(self.key(actor), -1) == turn

    def basic_choice(self, actor, choice):
        target = self.fighter_for_key(choice.target)
        friendly = target in self.living(0) and target is not actor
        return replace(choice, action=ACTION_ATTACK, skill_slot=None,
                       target=choice.target if friendly else None)


    def resolve(self, use_defaults=False):
        # Converted defense/support must lose its preparation initiative priority.
        for p in self.living(0):
            if self.forced(p, self.planning_round) and p.user_id in self.choices:
                self.choices[p.user_id] = self.basic_choice(p, self.choices[p.user_id])
        return super().resolve(use_defaults)

    def _resolve_player(self, actor, choice):
        if self.forced(actor, self.round):
            choice = self.basic_choice(actor, choice)
            if not any(p is not actor for p in self.living(0)):
                self.events['forced_friendly_no_target'] += 1
                return
            self.events['forced_friendly_actions'] += 1
        return super()._resolve_player(actor, choice)
