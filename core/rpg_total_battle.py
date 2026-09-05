"""Interactive, simultaneous-choice battle engine for total raids."""
from dataclasses import asdict, dataclass

from core.rpg_battle import (
    ALLY_EFFECTS,
    FIXED_TARGETS,
    Battle,
    Fighter,
    Rule,
    dump_battle,
    load_battle,
    rule_skill,
)


MAX_TOTAL_RAID_PLAYERS = 6
ACTION_ATTACK = 'attack'
ACTION_SKILL = 'skill'


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

    def __init__(self, fighters, seed=None, max_rounds=20, choices=None):
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
        for rule in sorted(actor.rules, key=lambda item: item.slot):
            skill = rule_skill(actor.job, rule)
            ready_round = actor.ready.get(rule.slot, 0)
            actions.append(dict(action=ACTION_SKILL, name=skill.name, skill_slot=rule.slot,
                                description=skill.description,
                                cooldown_remaining=max(0, ready_round - self.planning_round),
                                fixed_target=FIXED_TARGETS.get(skill.effect)))
        return actions

    def valid_targets(self, user_id, action, skill_slot=None):
        actor = self._player(user_id)
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
        if action not in (ACTION_ATTACK, ACTION_SKILL):
            raise TotalRaidError('請選擇普通攻擊或技能。')
        if action == ACTION_ATTACK:
            skill_slot = None
            self._validate_target(actor, target, enemy=True)
        else:
            rule, skill = self._skill(actor, skill_slot)
            if self.planning_round < actor.ready.get(rule.slot, 0):
                remaining = actor.ready[rule.slot] - self.planning_round
                raise TotalRaidError(f'【{skill.name}】仍需等待 {remaining} 回合。')
            if skill.effect in FIXED_TARGETS:
                target = None
            else:
                self._validate_target(actor, target, enemy=skill.effect not in ALLY_EFFECTS)
        choice = ActionChoice(user_id, action, target, skill_slot)
        self.choices[user_id] = choice
        return choice

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

    def intent(self):
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
        self.round += 1
        self.log.append(f'── 第 {self.round} 回合 ──')
        order = [fighter for fighter in self.fighters if fighter.hp > 0]
        self.rng.shuffle(order)
        order.sort(key=lambda fighter: fighter.dexterity, reverse=True)
        for actor in order:
            if actor.hp <= 0:
                continue
            if not self._begin_actor_turn(actor):
                if self.check_end():
                    break
                continue
            if actor.team == 0:
                self._resolve_player(actor, choices[actor.user_id])
            elif actor.job == '訓練用假人':
                self._resolve_dummy(actor, intent)
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
        if not self.result and self.round >= self.max_rounds:
            self.result = '平手（達回合上限）'
        return self.result

    def _begin_actor_turn(self, actor):
        """Apply persistent statuses before the actor's selected action."""
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
        dexterity=10_000, rules=[], armed=True, user_id=-1,
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


def dump_total_battle(battle):
    data = dump_battle(battle)
    data['choices'] = [asdict(choice) for choice in battle.choices.values()]
    data['mode'] = 'total_raid'
    return data


def load_total_battle(data):
    base = load_battle(data)
    choices = {item['user_id']: ActionChoice(**item) for item in data.get('choices', [])}
    battle = TotalRaidBattle(base.fighters, max_rounds=base.max_rounds, choices=choices)
    battle.round, battle.result, battle.log = base.round, base.result, base.log
    battle.mechanics = base.mechanics
    battle.rng.setstate(base.rng.getstate())
    return battle
