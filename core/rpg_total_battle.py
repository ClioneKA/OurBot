"""Interactive, simultaneous-choice battle engine for total raids."""
from dataclasses import asdict, dataclass

from core.rpg_battle import (
    ALLY_EFFECTS,
    FIXED_TARGETS,
    PREPARATION_TIMING,
    Battle,
    Fighter,
    Rule,
    dump_battle,
    load_battle,
    rule_skill,
    skill_description,
)


MAX_TOTAL_RAID_PLAYERS = 6
ACTION_ATTACK = 'attack'
ACTION_SKILL = 'skill'
ACTION_CANVAS = 'canvas'
PAINT_BITS = {'red': 1, 'yellow': 2, 'blue': 4}
PAINT_NAMES = {
    0: '無', 1: '紅色', 2: '黃色', 3: '橙色',
    4: '藍色', 5: '紫色', 6: '綠色', 7: '黑色',
}
PAINT_EFFECTS = {
    1: '傷害+15%',
    2: '速度+15、命中+5百分點',
    3: '傷害+25%、命中+10百分點、暴擊+10百分點',
    4: '受到傷害-15%、治療量+15%',
    5: '傷害+15%、受到傷害-25%、受治療量+20%',
    6: '速度+20、治療量+30%、免疫暈眩',
    7: '使諾亞停止一回合',
}
NOAH_JOB = '繪畫魔女．城崎諾亞'


class TotalRaidError(ValueError):
    """A player-safe invalid total-raid action."""


@dataclass(frozen=True)
class ActionChoice:
    user_id: int
    action: str
    target: str | None = None
    skill_slot: int | None = None
    automatic: bool = False


@dataclass(frozen=True)
class EnemyIntent:
    round: int
    name: str
    description: str
    target: str | None = None


class TotalRaidBattle(Battle):
    """Battle that pauses between rounds until every living player acts."""

    def __init__(self, fighters, seed=None, max_rounds=20, choices=None, paint_gifts=None):
        players = [fighter for fighter in fighters if fighter.team == 0]
        if not 1 <= len(players) <= MAX_TOTAL_RAID_PLAYERS:
            raise TotalRaidError(f'總力戰人數必須介於 1–{MAX_TOTAL_RAID_PLAYERS} 人。')
        if any(player.user_id is None for player in players):
            raise TotalRaidError('總力戰玩家必須具有使用者 ID。')
        if len({player.user_id for player in players}) != len(players):
            raise TotalRaidError('總力戰玩家 ID 不可重複。')
        if not any(fighter.team == 1 for fighter in fighters):
            raise TotalRaidError('總力戰至少需要一名敵人。')
        super().__init__(fighters, seed=seed, max_rounds=max_rounds)
        self.choices = dict(choices or {})
        self.paint_gifts = {int(user_id): target for user_id, target in (paint_gifts or {}).items()}

    @property
    def planning_round(self):
        return self.round + 1

    @staticmethod
    def player_key(fighter):
        return f'p:{fighter.user_id}'

    def key(self, fighter):
        if fighter.team == 0:
            return self.player_key(fighter)
        return next((f'e:{index}' for index, candidate in enumerate(self.fighters)
                     if candidate is fighter), None)

    def fighter_for_key(self, key):
        if not isinstance(key, str):
            return None
        if key.startswith('p:'):
            try:
                user_id = int(key[2:])
            except ValueError:
                return None
            return next((f for f in self.fighters if f.team == 0 and f.user_id == user_id), None)
        if key.startswith('e:'):
            try:
                index = int(key[2:])
            except ValueError:
                return None
            if not 0 <= index < len(self.fighters):
                return None
            fighter = self.fighters[index]
            return fighter if fighter.team == 1 else None
        return None

    def living_player_ids(self):
        return {fighter.user_id for fighter in self.living(0)}

    def waiting_player_ids(self):
        return self.living_player_ids() - set(self.choices)

    def available_actions(self, user_id):
        """Return UI-ready attacks and currently usable equipped skills."""
        actor = self._player(user_id)
        actions = [dict(action=ACTION_ATTACK, name='普通攻擊', skill_slot=None,
                        description='對一名敵人造成 100% 傷害。')]
        if self.noah_phase() == 1:
            for color, name in (('red', '紅'), ('yellow', '黃'), ('blue', '藍')):
                actions.append(dict(action=ACTION_CANVAS, name=f'踏入畫布・{name}', skill_slot=None,
                                    canvas_color=color,
                                    description=f'放棄本回合行動，向畫布加入{name}色。'))
        for rule in sorted(actor.rules, key=lambda item: item.slot):
            skill = rule_skill(actor.job, rule)
            ready_round = actor.ready.get(rule.slot, 0)
            actions.append(dict(action=ACTION_SKILL, name=skill.name, skill_slot=rule.slot,
                                description=skill_description(skill),
                                cooldown_remaining=max(0, ready_round - self.planning_round),
                                fixed_target=FIXED_TARGETS.get(skill.effect)))
        return actions

    def valid_targets(self, user_id, action, skill_slot=None):
        actor = self._player(user_id)
        if action == ACTION_CANVAS:
            return []
        if action == ACTION_ATTACK:
            return [self.key(target) for target in self.living(1)]
        rule, skill = self._skill(actor, skill_slot)
        if skill.effect in FIXED_TARGETS:
            return []
        team = actor.team if skill.effect in ALLY_EFFECTS else 1 - actor.team
        return [self.key(target) for target in self.living(team)]

    def submit(self, user_id, action, target=None, skill_slot=None):
        if self.result:
            raise TotalRaidError('總力戰已經結束。')
        actor = self._player(user_id)
        if actor.hp <= 0:
            raise TotalRaidError('倒下的玩家無法選擇行動。')
        if action not in (ACTION_ATTACK, ACTION_SKILL, ACTION_CANVAS):
            raise TotalRaidError('請選擇普通攻擊、技能或特殊行動。')
        if action == ACTION_ATTACK:
            skill_slot = None
            self._validate_target(actor, target, enemy=True)
        elif action == ACTION_SKILL:
            rule, skill = self._skill(actor, skill_slot)
            if self.planning_round < actor.ready.get(rule.slot, 0):
                remaining = actor.ready[rule.slot] - self.planning_round
                raise TotalRaidError(f'【{skill.name}】仍需等待 {remaining} 回合。')
            if skill.effect in FIXED_TARGETS:
                target = None
            else:
                self._validate_target(actor, target, enemy=skill.effect not in ALLY_EFFECTS)
        else:
            if self.noah_phase() != 1 or target not in PAINT_BITS:
                raise TotalRaidError('目前無法踏入畫布，或選擇的顏色無效。')
            skill_slot = None
        choice = ActionChoice(user_id, action, target, skill_slot)
        self.choices[user_id] = choice
        return choice

    def submit_paint_gift(self, user_id, target):
        if self.noah_phase() != 2:
            raise TotalRaidError('只有第二階段可以給予顏料。')
        actor = self._player(user_id)
        if not actor.status_stacks.get('paint_mask', 0):
            raise TotalRaidError('你目前沒有可以給予的顏料。')
        receiver = self._validate_target(actor, target, enemy=False)
        if receiver is actor:
            raise TotalRaidError('顏料必須給予另一名隊友。')
        self.paint_gifts[user_id] = target
        return target

    def noah(self):
        return next((fighter for fighter in self.fighters
                     if fighter.team == 1 and fighter.job == NOAH_JOB), None)

    def noah_phase(self):
        return int(self.mechanics.get('noah_total_phase', 0)) if self.noah() else 0

    @staticmethod
    def paint_mask(fighter):
        return int(fighter.status_stacks.get('paint_mask', 0))

    def accuracy_bonus(self, actor):
        return {2: 5, 3: 10, 6: 0}.get(self.paint_mask(actor), 0)

    def critical_bonus(self, actor):
        return 10 if self.paint_mask(actor) == 3 else 0

    def damage_dealt_multiplier(self, actor):
        if actor.job == NOAH_JOB:
            return 1 + self.mechanics.get('noah_source_stacks', 0) * 0.1
        return {1: 1.15, 3: 1.25, 5: 1.15}.get(self.paint_mask(actor), 1.0)

    def damage_taken_multiplier(self, target):
        if self.mechanics.get('noah_contrast_active') and target.team == 0:
            return 0.3
        return {4: 0.85, 5: 0.75}.get(self.paint_mask(target), 1.0)

    def healing_done_multiplier(self, actor):
        return {4: 1.15, 6: 1.3}.get(self.paint_mask(actor), 1.0)

    def healing_received_multiplier(self, target):
        return 1.2 if self.paint_mask(target) == 5 else 1.0

    def effective_speed(self, fighter):
        return fighter.speed + {2: 15, 6: 20}.get(self.paint_mask(fighter), 0)

    def fill_defaults(self):
        """Give timed-out players a basic attack against the lowest-HP enemy."""
        enemies = self.living(1)
        if not enemies:
            return []
        target = min(enemies, key=lambda fighter: fighter.hp / fighter.stats['HP'])
        added = []
        for user_id in sorted(self.waiting_player_ids()):
            choice = ActionChoice(user_id, ACTION_ATTACK, self.key(target), automatic=True)
            self.choices[user_id] = choice
            added.append(choice)
        return added

    def ready_to_resolve(self):
        return not self.result and not self.waiting_player_ids()

    def resolve(self, use_defaults=False):
        if self.result or self.check_end():
            return self.result
        if use_defaults:
            self.fill_defaults()
        waiting = self.waiting_player_ids()
        if waiting:
            raise TotalRaidError(f'尚有 {len(waiting)} 名玩家未選擇行動。')
        intent = self.intent()
        choices = dict(self.choices)
        self.choices.clear()
        round_log_start = len(self.log)
        self.round += 1
        self.log.append(f'── 第 {self.round} 回合 ──')
        black_pause = self._resolve_paint_gifts() if self.noah_phase() == 2 else False
        scheduled_pause = bool(self.mechanics.pop('noah_pause_current', False))
        paused_this_round = black_pause or scheduled_pause
        if self.noah_phase() == 1:
            self._resolve_canvas(choices, intent)
        order = [fighter for fighter in self.fighters if fighter.hp > 0]
        self.rng.shuffle(order)

        def priority(fighter):
            if fighter.team != 0:
                return 0
            choice = choices.get(fighter.user_id)
            if choice is None or choice.action != ACTION_SKILL:
                return 0
            _, skill = self._skill(fighter, choice.skill_slot)
            target = self.fighter_for_key(choice.target)
            if skill.effect == 'cleanse' and target is not None and target.status_stacks.get('source_erosion'):
                return 2
            return int(skill.timing == PREPARATION_TIMING)

        order.sort(key=lambda fighter: (priority(fighter), self.effective_speed(fighter)), reverse=True)
        for actor in order:
            if actor.hp <= 0:
                continue
            if not self._begin_actor_turn(actor):
                if self.check_end():
                    break
                continue
            if actor.team == 0:
                if choices[actor.user_id].action != ACTION_CANVAS:
                    self._resolve_player(actor, choices[actor.user_id])
            elif actor.job == '訓練用假人':
                self._resolve_dummy(actor, intent)
            elif actor.job == NOAH_JOB:
                if paused_this_round:
                    self.log.append(f'{actor.name} 被【黑色】覆蓋，本回合停止行動。')
                else:
                    self._resolve_noah(actor, intent)
            else:
                self.act(actor)
            if self.check_end():
                break
        if not self.result:
            for fighter in self.living(0):
                if fighter.food_regen_left and self.round >= fighter.food_regen_start:
                    amount = self.restore(fighter, max(1, fighter.stats['HP'] * fighter.food_regen_permille // 1000))
                    fighter.food_regen_left -= 1
                    self.log.append(f'{fighter.name} 的【{fighter.food_name}】緩補恢復 {amount} HP。')
        self._expire_source_erosion()
        if self.noah() is not None and not self.result:
            changed_phase = self._sync_noah_phase()
            if changed_phase or not paused_this_round:
                self._prepare_noah_intent()
        if not self.result and self.round >= self.max_rounds:
            self.result = '平手（達回合上限）'
        self.mechanics['last_round_log'] = self.log[round_log_start:]
        return self.result

    def _begin_actor_turn(self, actor):
        """Apply persistent statuses before the actor's selected action."""
        if actor.job == NOAH_JOB:
            if actor.effects.pop('stun', None) is not None:
                self.log.append(f'{actor.name} 免疫暈眩；只有黑色能使她停止行動。')
        if actor.team == 0 and actor.status_stacks.get('corruption', 0) >= 3:
            actor.status_stacks.pop('corruption', None)
            damage = max(1, actor.stats['HP'] * 12 // 100)
            actual = min(actor.hp, damage)
            actor.hp -= actual
            actor.combat_stats['damage_taken'] += actual
            if actual and actor.hp == 0:
                actor.combat_stats['deaths'] += 1
            self.log.append(f'{actor.name} 的【腐敗爆裂】：自身損失 {actual} HP，腐敗歸零。')
            self.maybe_eat(actor)
            for ally in [fighter for fighter in self.living(0) if fighter is not actor]:
                splash = max(1, ally.stats['HP'] * 3 // 100)
                taken = min(ally.hp, splash)
                ally.hp -= taken
                ally.combat_stats['damage_taken'] += taken
                if taken and ally.hp == 0:
                    ally.combat_stats['deaths'] += 1
                self.log.append(f'{ally.name} 受到腐敗波及，損失 {taken} HP。')
                self.maybe_eat(ally)
            if actor.hp <= 0:
                return False
        if actor.status_stacks.get('poison_arrows') and not self.tick_poison_arrows(actor):
            return False
        if actor.has('poison', self.round):
            damage = max(1, actor.stats['HP'] // (20 if actor.team == 0 else 50))
            actual = min(actor.hp, damage)
            actor.hp -= actual
            actor.combat_stats['damage_taken'] += actual
            source = self.effect_source(actor, 'poison')
            if source is not None and source is not actor:
                source.combat_stats['damage_dealt'] += actual
                source.combat_stats['support_damage'] += actual
            if actual and actor.hp == 0:
                actor.combat_stats['deaths'] += 1
                if source is not None and source is not actor:
                    source.combat_stats['knockouts'] += 1
            self.log.append(f'{actor.name} 中毒，損失 {damage} HP')
            self.maybe_eat(actor)
            if actor.hp <= 0:
                return False
        if actor.has('stun', self.round) and self.paint_mask(actor) == 6:
            actor.effects.pop('stun', None)
            self.log.append(f'{actor.name} 受到【綠色】保護，免疫暈眩。')
        if actor.has('stun', self.round):
            actor.effects.pop('stun', None)
            self.log.append(f'{actor.name} 因暈眩跳過本次行動。')
            return False
        return True

    def _player(self, user_id):
        actor = next((fighter for fighter in self.fighters
                      if fighter.team == 0 and fighter.user_id == user_id), None)
        if actor is None:
            raise TotalRaidError('你不在這場總力戰中。')
        return actor

    @staticmethod
    def _skill(actor, skill_slot):
        if type(skill_slot) is not int:
            raise TotalRaidError('請選擇技能。')
        rule = next((rule for rule in actor.rules if rule.slot == skill_slot), None)
        if rule is None:
            raise TotalRaidError('這個技能沒有裝備。')
        return rule, rule_skill(actor.job, rule)

    def _validate_target(self, actor, key, enemy):
        target = self.fighter_for_key(key)
        expected_team = 1 - actor.team if enemy else actor.team
        if target is None or target.team != expected_team or target.hp <= 0:
            raise TotalRaidError('選擇的目標無效或已經倒下。')
        return target

    def _fallback_target(self, actor, choice, enemy):
        expected_team = 1 - actor.team if enemy else actor.team
        target = self.fighter_for_key(choice.target)
        if target is not None and target.team == expected_team and target.hp > 0:
            return target
        candidates = self.living(expected_team)
        if not candidates:
            return None
        return min(candidates, key=lambda fighter: fighter.hp / fighter.stats['HP'])

    def _resolve_player(self, actor, choice):
        if choice.automatic:
            self.log.append(f'{actor.name} 未及時選擇，改為普通攻擊。')
        if choice.action == ACTION_ATTACK:
            target = self._fallback_target(actor, choice, enemy=True)
            self.record_skill(actor, '普通攻擊')
            self.log.append(f'{actor.name} 使用普通攻擊')
            if target is not None:
                self.hit(actor, target)
            return
        rule, skill = self._skill(actor, choice.skill_slot)
        if skill.effect in FIXED_TARGETS:
            target = actor
        else:
            target = self._fallback_target(actor, choice, enemy=skill.effect not in ALLY_EFFECTS)
        if target is not None:
            self.use_skill(actor, rule, skill, target)

    def use_skill(self, actor, rule, skill, target):
        erosion = bool(skill.effect == 'cleanse' and target.status_stacks.get('source_erosion'))
        super().use_skill(actor, rule, skill, target)
        if erosion:
            target.status_stacks.pop('source_erosion', None)
            self.mechanics['noah_source_stacks'] = self.mechanics.get('noah_source_stacks', 0) + 1
            self.log.append(f'【源色侵蝕】被淨化；{self.noah().name} 的源色增加至 '
                            f'{self.mechanics["noah_source_stacks"]} 層。')

    def _resolve_canvas(self, choices, intent):
        mask = 0
        entrants = []
        for fighter in self.living(0):
            choice = choices.get(fighter.user_id)
            if choice is not None and choice.action == ACTION_CANVAS:
                mask |= PAINT_BITS[choice.target]
                entrants.append(f'{fighter.name}({PAINT_NAMES[PAINT_BITS[choice.target]]})')
        result = PAINT_NAMES[mask]
        self.log.append(f'畫布混色：{"、".join(entrants) if entrants else "無人踏入"} → {result}。')
        contrasts = {'white': 7, 'red': 6, 'yellow': 5, 'blue': 3,
                     'orange': 4, 'green': 1, 'purple': 2, 'black': 0}
        correct = bool(intent and contrasts.get(self.mechanics.get('noah_intent_color')) == mask)
        self.mechanics['noah_contrast_active'] = correct
        self.log.append('對比色成立，本回合顏色攻擊傷害降低 70%。' if correct else '對比色未成立。')

    def _resolve_paint_gifts(self):
        incoming = {}
        transfers = []
        for user_id, target_key in list(self.paint_gifts.items()):
            giver = next((f for f in self.living(0) if f.user_id == user_id), None)
            receiver = self.fighter_for_key(target_key)
            mask = self.paint_mask(giver) if giver is not None else 0
            if not mask or receiver is None or receiver.team != 0 or receiver.hp <= 0 or receiver is giver:
                continue
            giver.status_stacks.pop('paint_mask', None)
            incoming[receiver.user_id] = incoming.get(receiver.user_id, 0) | mask
            transfers.append(f'{giver.name} → {receiver.name}（{PAINT_NAMES[mask]}）')
        self.paint_gifts.clear()
        black = False
        for user_id, mask in incoming.items():
            receiver = next(f for f in self.living(0) if f.user_id == user_id)
            mixed = self.paint_mask(receiver) | mask
            receiver.status_stacks['paint_mask'] = mixed
            black |= mixed == 7
        if transfers:
            self.log.append('給予顏料：' + '、'.join(transfers) + '。')
        if black:
            self._clear_paints()
            self.log.append('三原色混成【黑色】：全隊顏料清空，諾亞本回合停止行動！')
        return black

    def _clear_paints(self):
        for fighter in self.fighters:
            fighter.status_stacks.pop('paint_mask', None)

    def _resolve_noah(self, actor, intent):
        phase = self.noah_phase()
        self.record_skill(actor, intent.name)
        self.log.append(f'{actor.name} 使用【{intent.name}】')
        if phase == 1:
            color = self.mechanics['noah_intent_color']
            power = {'white': .7, 'red': .9, 'yellow': .9, 'blue': .9,
                     'orange': 1.1, 'green': 1.1, 'purple': 1.1, 'black': 1.5}[color]
            for target in self.living(0):
                self.hit(actor, target, power)
            self.mechanics['noah_contrast_active'] = False
            return
        if phase == 2:
            self.mechanics['noah_phase_round'] = self.mechanics.get('noah_phase_round', 0) + 1
            target = self._announced_target(intent.target)
            if target is not None:
                had_green = self.paint_mask(target) == 6
                original_defense = target.stats['防禦']
                target.stats['防禦'] = 0
                try:
                    hit = self.hit(actor, target, 1.0)
                finally:
                    target.stats['防禦'] = original_defense
                if hit and target.hp > 0:
                    self._grant_source_paint(target, self.mechanics['noah_intent_color'])
                    if not had_green and self.rng.random() < .2:
                        if self.apply_debuff(target, 'stun', self.round + 1, actor):
                            self.log.append(f'{target.name} 被源色震懾，將跳過下一次行動。')
            if self.mechanics['noah_phase_round'] % 3 == 0:
                self.mechanics['noah_source_stacks'] = self.mechanics.get('noah_source_stacks', 0) + 1
                self._clear_paints()
                self.log.append(f'{actor.name} 同回合施放【源色解放】：源色變為 '
                                f'{self.mechanics["noah_source_stacks"]} 層，全隊顏料清空。')
            return
        for target_key in self.mechanics.get('noah_intent_targets', []):
            if actor.hp <= 0 or not self.living(0):
                break
            target = self._announced_target(target_key)
            if target is None:
                continue
            eroded = bool(target.status_stacks.get('source_erosion'))
            hit = self.hit(actor, target, .75)
            if hit and eroded:
                if target.hp > 0:
                    erased = target.hp
                    target.hp = 0
                    target.combat_stats['damage_taken'] += erased
                    actor.combat_stats['damage_dealt'] += erased
                    actor.combat_stats['direct_damage'] += erased
                    actor.combat_stats['knockouts'] += 1
                    target.combat_stats['deaths'] += 1
                self.log.append(f'{target.name} 被【源色侵蝕】抹除！')
        self.mechanics['noah_phase_round'] = self.mechanics.get('noah_phase_round', 0) + 1

    def _announced_target(self, target_key):
        candidates = self.living(0)
        if not candidates:
            return None
        taunters = [fighter for fighter in candidates if fighter.has('taunt', self.round)]
        if taunters:
            return min(taunters, key=lambda fighter: fighter.hp / fighter.stats['HP'])
        target = self.fighter_for_key(target_key)
        return target if target in candidates else self.rng.choice(candidates)

    def _grant_source_paint(self, target, color):
        mixed = self.paint_mask(target) | PAINT_BITS[color]
        target.status_stacks['paint_mask'] = mixed
        self.log.append(f'{target.name} 獲得顏料，現在是【{PAINT_NAMES[mixed]}】。')
        if mixed == 7:
            self._clear_paints()
            self.mechanics['noah_pause_current'] = True
            self.log.append('三原色混成【黑色】：全隊顏料清空，諾亞將在下一回合停止行動！')

    def _expire_source_erosion(self):
        for fighter in self.fighters:
            fighter.status_stacks.pop('source_erosion', None)

    def _sync_noah_phase(self):
        actor = self.noah()
        old = self.noah_phase()
        phase = 3 if actor.hp * 100 <= actor.stats['HP'] * 35 else 2 if actor.hp * 100 <= actor.stats['HP'] * 70 else 1
        if phase == old:
            return False
        self.mechanics.update(noah_total_phase=phase, noah_phase_round=0,
                              noah_intent_color=None, noah_intent_target=None,
                              noah_intent_targets=[])
        self.paint_gifts.clear()
        if phase == 2:
            self.log.append(f'{actor.name} 將全隊拉入畫布，進入第二階段【源色攻擊】！')
        else:
            self._clear_paints()
            self.log.append(f'{actor.name} 進入第三階段【源色侵蝕】，每回合連續攻擊三次！')
        return True

    def _prepare_noah_intent(self):
        actor = self.noah()
        if actor is None or actor.hp <= 0 or self.result:
            return
        phase = self.noah_phase()
        if phase == 1:
            color = self.rng.choice(('white', 'red', 'yellow', 'blue', 'orange', 'green', 'purple', 'black'))
            self.mechanics['noah_intent_color'] = color
        elif phase == 2:
            bag = list(self.mechanics.get('noah_source_bag', []))
            if not bag:
                bag = ['red', 'yellow', 'blue']
                self.rng.shuffle(bag)
            self.mechanics['noah_intent_color'] = bag.pop()
            self.mechanics['noah_source_bag'] = bag
            self.mechanics['noah_intent_target'] = self.key(self.rng.choice(self.living(0)))
        else:
            next_round = self.mechanics.get('noah_phase_round', 0) + 1
            living = self.living(0)
            if next_round % 3 == 0 and living:
                marked = self.rng.choice(living)
                marked.status_stacks['source_erosion'] = 1
                self.mechanics['noah_erosion_target'] = self.key(marked)
            else:
                self.mechanics['noah_erosion_target'] = None
            marked_key = self.mechanics.get('noah_erosion_target')
            targets = [self.key(self.rng.choice(living)) for _ in range(3)] if living else []
            if marked_key and self.fighter_for_key(marked_key) in living and targets:
                targets[self.rng.randrange(3)] = marked_key
            self.mechanics['noah_intent_targets'] = targets

    def intent(self):
        noah = self.noah()
        if noah is not None:
            if not self.mechanics.get('noah_intent_color') and not self.mechanics.get('noah_intent_targets'):
                self._prepare_noah_intent()
            phase = self.noah_phase()
            if self.mechanics.get('noah_pause_current'):
                return EnemyIntent(self.planning_round, '黑色暫停', '受到黑色影響，本回合不會行動。')
            if phase == 1:
                names = {'white': '白', 'red': '紅', 'yellow': '黃', 'blue': '藍',
                         'orange': '橙', 'green': '綠', 'purple': '紫', 'black': '黑'}
                color = self.mechanics['noah_intent_color']
                contrast = {'white': '黑', 'red': '綠', 'yellow': '紫', 'blue': '橙',
                            'orange': '藍', 'green': '紅', 'purple': '黃', 'black': '白'}[color]
                return EnemyIntent(self.planning_round, f'{names[color]}色攻擊',
                                   f'對全隊發動顏色攻擊；本回合需混出{contrast}色，可降低 70% 傷害。')
            if phase == 2:
                names = {'red': '紅', 'yellow': '黃', 'blue': '藍'}
                target = self.fighter_for_key(self.mechanics.get('noah_intent_target'))
                liberation = (self.mechanics.get('noah_phase_round', 0) + 1) % 3 == 0
                extra = '；命中後同回合施放源色解放。' if liberation else '。'
                return EnemyIntent(self.planning_round, f'{names[self.mechanics["noah_intent_color"]]}色源色攻擊',
                                   f'對 {target.name if target else "隨機目標"} 造成 100% 穿防傷害，命中後取得顏料並有 20% 機率暈眩{extra}',
                                   self.mechanics.get('noah_intent_target'))
            targets = [self.fighter_for_key(key) for key in self.mechanics.get('noah_intent_targets', [])]
            names = '、'.join(target.name for target in targets if target is not None)
            marked = next((f.name for f in self.living(0) if f.status_stacks.get('source_erosion')), None)
            warning = f'；{marked} 帶有源色侵蝕，命中即被抹除' if marked else ''
            return EnemyIntent(self.planning_round, '繪除・三連擊', f'依序攻擊：{names}{warning}。')
        return self._dummy_intent()

    def _dummy_intent(self):
        """Return the training dummy's exact next action for the planning UI."""
        dummy = next((fighter for fighter in self.living(1) if fighter.job == '訓練用假人'), None)
        if dummy is None:
            return None
        round_number = self.planning_round
        mode = (round_number - 1) % 4
        players = self.living(0)
        if mode == 0:
            target = min(players, key=lambda f: f.hp / f.stats['HP'])
            return EnemyIntent(round_number, '標準打擊', f'對 {target.name} 造成 100% 單體傷害。', self.key(target))
        if mode == 1:
            return EnemyIntent(round_number, '防禦校準', '本回合不攻擊，受到的傷害降低 35%。')
        if mode == 2:
            return EnemyIntent(round_number, '廣域震波', '對所有存活玩家造成 65% 傷害。')
        target = max(players, key=lambda f: (f.stats['攻擊'], -f.user_id))
        return EnemyIntent(round_number, '過載重擊', f'對 {target.name} 造成 180% 單體傷害。', self.key(target))

    def _resolve_dummy(self, actor, intent):
        if intent is None:
            return
        self.record_skill(actor, intent.name)
        self.log.append(f'{actor.name} 使用【{intent.name}】')
        mode = (self.round - 1) % 4
        if mode == 1:
            actor.effects['stance'] = self.round
            return
        if mode == 2:
            for target in self.living(0):
                self.hit(actor, target, 0.65)
            return
        target = self.fighter_for_key(intent.target)
        if target is None or target.hp <= 0:
            candidates = self.living(0)
            target = min(candidates, key=lambda f: f.hp / f.stats['HP']) if candidates else None
        if target is not None:
            self.hit(actor, target, 1.8 if mode == 3 else 1.0)


def training_dummy_battle(players, seed=None, max_rounds=20):
    """Build a test battle from already snapshotted player fighters."""
    if not players:
        raise TotalRaidError('至少需要一名玩家。')
    average_hp = sum(player.stats['HP'] for player in players) // len(players)
    average_attack = sum(player.stats['攻擊'] for player in players) // len(players)
    dummy = Fighter(
        '訓練用假人', 1, '訓練用假人',
        {'HP': max(500, average_hp * len(players) * 4),
         '攻擊': max(25, average_attack), '防禦': 20, '治療量': 0,
         '命中率': 100, '閃避率': 0, '暴擊率': 0},
        # The dummy deliberately acts first so its announced defensive stance
        # protects the whole round and its target does not change beforehand.
        speed=100, rules=[], armed=True, user_id=-1,
    )
    return TotalRaidBattle([*players, dummy], seed=seed, max_rounds=max_rounds)


def training_dummy_battle_from_participants(participants, seed=None, max_rounds=20):
    """Build the interactive test fight from the same snapshots as raids."""
    from core.rpg_battle import raid_battle

    snapshot = raid_battle(
        participants,
        {'kind': '訓練用假人', 'name': '訓練用假人', 'strength': 1.0},
        seed,
    )
    battle = training_dummy_battle(
        [fighter for fighter in snapshot.fighters if fighter.team == 0],
        seed=seed,
        max_rounds=max_rounds,
    )
    battle.log.extend(snapshot.log)
    return battle


def noah_total_battle(players, seed=None, max_rounds=30, *, hp_per_player=14_000,
                      attack=550, defense=340):
    """Build the fixed-stat painting-witch encounter; player level never scales it."""
    if not players:
        raise TotalRaidError('至少需要一名玩家。')
    noah = Fighter(
        NOAH_JOB, 1, NOAH_JOB,
        {'HP': hp_per_player * len(players), '攻擊': attack, '防禦': defense, '治療量': 0,
         '命中率': 110, '閃避率': 10, '暴擊率': 10},
        speed=65, rules=[], armed=True, user_id=-1,
    )
    battle = TotalRaidBattle([*players, noah], seed=seed, max_rounds=max_rounds)
    battle.mechanics.update(noah_total_phase=1, noah_phase_round=0,
                            noah_source_stacks=0, noah_source_bag=[],
                            noah_intent_color=None, noah_intent_target=None,
                            noah_intent_targets=[], noah_erosion_target=None)
    battle._prepare_noah_intent()
    return battle


def noah_total_battle_from_participants(participants, seed=None, max_rounds=30):
    """Build Noah from real character snapshots and their prepared provisions."""
    from core.rpg_battle import raid_battle

    snapshot = raid_battle(
        participants,
        {'kind': '總力戰參戰資料', 'name': '總力戰參戰資料', 'strength': 1.0},
        seed,
    )
    battle = noah_total_battle(
        [fighter for fighter in snapshot.fighters if fighter.team == 0],
        seed=seed, max_rounds=max_rounds,
    )
    battle.log.extend(snapshot.log)
    return battle


def dump_total_battle(battle):
    data = dump_battle(battle)
    data['choices'] = [asdict(choice) for choice in battle.choices.values()]
    data['paint_gifts'] = battle.paint_gifts
    data['mode'] = 'total_raid'
    return data


def load_total_battle(data):
    base = load_battle(data)
    choices = {item['user_id']: ActionChoice(**item) for item in data.get('choices', [])}
    battle = TotalRaidBattle(base.fighters, max_rounds=base.max_rounds, choices=choices,
                             paint_gifts=data.get('paint_gifts', {}))
    battle.round, battle.result, battle.log = base.round, base.result, base.log
    battle.mechanics = base.mechanics
    battle.rng.setstate(base.rng.getstate())
    return battle
