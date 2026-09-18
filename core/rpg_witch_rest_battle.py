"""Witch Rest battles using the existing automatic and manual raid engines."""
from core.rpg_battle import Battle, Fighter, Rule, participant_fighters, raid_battle
from core.rpg_character import CharacterError
from core.rpg_total_battle import ACTION_ATTACK, ACTION_SKILL, ActionChoice, EnemyIntent
from core.rpg_witch_battle import ACTION_DEFEND, WitchRaidBattle
from core.rpg_witch_rest import WITCHES


PANEL_STATS = {
    'ema': (81_000, 1_462, 470),
    'hiro': (67_500, 1_440, 470),
}

MANUAL_STATS = {
    'ema': {
        100: (81_000, 1_462, 470), 250: (83_700, 1_511, 480),
        500: (85_500, 1_544, 485), 750: (85_950, 1_552, 490),
        1_000: (90_000, 1_625, 500), 2_000: (112_500, 1_918, 600),
        4_000: (139_500, 2_242, 750),
    },
    'hiro': {
        100: (67_500, 1_440, 470), 250: (69_750, 1_488, 480),
        500: (71_250, 1_520, 485), 750: (71_625, 1_528, 490),
        1_000: (75_000, 1_600, 500), 2_000: (93_750, 1_888, 600),
        4_000: (116_250, 2_208, 750),
    },
}

REWRITE_ACTIONS = {
    'rewrite_attack': ('改寫過去・普通攻擊', 'attack'),
    'rewrite_skill': ('改寫過去・主動技能', 'skill'),
    'rewrite_defend': ('改寫過去・防禦', 'defend'),
}


def manual_stats(witch_id, enrage):
    """Linearly interpolate the approved four-player stat anchors."""
    anchors = MANUAL_STATS[witch_id]
    if enrage in anchors:
        return anchors[enrage]
    upper = next(value for value in anchors if value > enrage)
    lower = max(value for value in anchors if value < enrage)
    ratio = (enrage - lower) / (upper - lower)
    return tuple(round(start + (end - start) * ratio)
                 for start, end in zip(anchors[lower], anchors[upper]))


class WitchRestManualBattle(WitchRaidBattle):
    """Restartable one-witch battle; bespoke thresholds build on this shell."""
    expected_witches = 1

    def __init__(self, fighters, witch_id, enrage, seed=None, max_rounds=30):
        super().__init__(fighters, (witch_id,), seed=seed, max_rounds=max_rounds)
        self.witch_id = witch_id
        self.enrage = enrage
        self.mechanics.update(witch_rest_mode='manual', witch_id=witch_id, enrage=enrage)
        self._rest_player_actor = None
        self._rest_target_key = None
        self._rest_testimony_triggered = False

    @staticmethod
    def action_category(choice):
        if choice.action in REWRITE_ACTIONS:
            return REWRITE_ACTIONS[choice.action][1]
        return 'skill' if choice.action == ACTION_SKILL else choice.action

    def available_actions(self, user_id):
        actions = super().available_actions(user_id)
        final = self.mechanics.get('hiro_final')
        used = set(final.get('rewrite_used', [])) if final else set()
        if final and final.get('active') and user_id not in used:
            actions.extend(dict(action=action, name=name, skill_slot=None,
                                description='每場一次；不造成效果，但必定完成個人世界線偏差。')
                           for action, (name, _) in REWRITE_ACTIONS.items())
        return actions

    def valid_targets(self, user_id, action, skill_slot=None):
        if action in REWRITE_ACTIONS:
            return []
        targets = super().valid_targets(user_id, action, skill_slot)
        final = self.mechanics.get('ema_final')
        if final and final.get('active'):
            own = next((key for key, owner in final['shadows'].items()
                        if owner == user_id), None)
            targets = [key for key in targets if key != own]
        return targets

    def submit(self, user_id, action, target=None, skill_slot=None):
        if action not in REWRITE_ACTIONS:
            return super().submit(user_id, action, target, skill_slot)
        actor = self._player(user_id)
        final = self.mechanics.get('hiro_final')
        if (not final or not final.get('active') or actor.hp <= 0
                or user_id in final.get('rewrite_used', [])):
            raise CharacterError('目前無法使用改寫過去。')
        choice = ActionChoice(user_id, action)
        self.choices[user_id] = choice
        self.confirmed.discard(user_id)
        self.auto_players.discard(user_id)
        self.timeout_streak[user_id] = 0
        return choice

    def apply_damage(self, target, damage, share_link=True, direct=False):
        boss = self.witch(self.witch_id)
        final = self.mechanics.get('hiro_final')
        if target is boss and final and final.get('active'):
            damage = min(damage, max(0, target.hp - 1))
        if (target.status_stacks.get('rest_evidence')
                and self._rest_target_key != self.key(target)):
            damage = 0
        if target.status_stacks.get('rest_guilt_evidence'):
            damage = 0
        shield = self.mechanics.get('rest_boss_shield', 0) if target is boss else 0
        if shield:
            absorbed = min(shield, max(0, int(damage)))
            damage -= absorbed
            self.mechanics['rest_boss_shield'] = shield - absorbed
            self.log.append(f'{boss.name}的判決護盾吸收 {absorbed} 點傷害。')
        result = super().apply_damage(target, damage, share_link, direct)
        data = self.pending.get('ema')
        if (direct and self._rest_player_actor and not self._rest_testimony_triggered
                and data and data.get('kind') == 'rest_prosecution'
                and not data.get('testimony_broken') and self.key(target) in data['targets']):
            self._rest_testimony_triggered = True
            total = result[0] + result[2]
            percent = 40 if self.enrage >= 1_000 and self.enrage < 2_000 else 30
            for key in data['targets']:
                other = self.fighter_for_key(key)
                if other and other is not target and other.hp > 0:
                    echoed, _, shared = super().apply_damage(
                        other, total * percent // 100, share_link=False, direct=False)
                    self.log.append(f'共同證言使 {other.name}承受 {echoed + shared} 點共鳴傷害。')
        return result

    def damage_taken_multiplier(self, target):
        result = super().damage_taken_multiplier(target)
        boss = self.witch(self.witch_id)
        if target is boss:
            if self.mechanics.get('hiro_dislocation_until', -1) >= self.round:
                result *= .5
            if self.mechanics.get('rest_vulnerable_until', -1) >= self.round:
                result *= 1.15
        return result

    def check_end(self):
        if self.witch_id != 'hiro':
            return super().check_end()
        if self.inside_action:
            return False
        boss = self.witch('hiro')
        if boss and boss.hp <= 0 and self.living(0):
            rewinds = self.mechanics.get('hiro_rewinds', 0)
            if rewinds == 0 or rewinds == 1 and self.enrage >= 750:
                self._hiro_rewind(boss, rewinds + 1)
                return False
        if not self.living(0):
            self.result = '戰敗'
        elif not boss or boss.hp <= 0:
            self.result = '勝利'
            for fighter in self.living(1):
                fighter.hp = 0
        return self.result is not None

    def hit(self, actor, target, power=1.0, **kwargs):
        if actor.job == 'ema' and target.team == 0:
            power *= 1 + self.factor_stacks(target) * .04
        result = super().hit(actor, target, power, **kwargs)
        if actor.job == 'hiro' and target.team == 0 and result:
            attacks = self.mechanics.setdefault('hiro_attacks', [])
            attacks.append(dict(target=self.key(target), power=power, round=self.round))
            del attacks[:-20]
        return result

    def victim(self, actor):
        if actor.job == 'hiro':
            histories = self.mechanics.get('rest_action_history', {})
            skilled = [player for player in self.living(0)
                       if histories.get(str(player.user_id), [None])[-1:] == ['skill']]
            if skilled:
                return min(skilled, key=lambda player: player.user_id)
        return super().victim(actor)

    def hit_chance(self, actor, target):
        if target.status_stacks.get('rest_evidence'):
            return 100
        return super().hit_chance(actor, target)

    def apply_debuff(self, target, effect, until, source=None):
        data = self.pending.get('ema')
        if (target.job == 'ema' and effect == 'stun' and data
                and data.get('kind') == 'rest_guilt'):
            self.log.append(f'{target.name}的【有罪推定】不可異議。')
            return False
        if (target.job == 'ema' and effect == 'stun' and data
                and data.get('kind') == 'rest_prosecution'
                and data.get('objection_resist', 0)):
            data['objection_resist'] -= 1
            self.log.append(f'{target.name}的【異議棄卻】擋下本次異議。')
            return False
        if target.status_stacks.get('rest_evidence') and effect in ('stun', 'weak', 'break'):
            self.log.append(f'{target.name}不受控制。')
            return False
        return super().apply_debuff(target, effect, until, source)

    def clear_negative_effects(self, target):
        stacks = self.factor_stacks(target)
        data = self.pending.get('ema')
        if (stacks and data and data.get('kind') == 'rest_prosecution'
                and self.key(target) in data['targets']):
            data['testimony_broken'] = True
        protected = target.status_stacks.get('rest_factor_preservation', 0)
        if not stacks or not protected:
            return super().clear_negative_effects(target)
        until = target.effects.get('factor', self.round + 30)
        removed = super().clear_negative_effects(target)
        target.status_stacks['factor'] = stacks
        target.effects['factor'] = until
        target.status_stacks['rest_factor_preservation'] = protected - 1
        self.log.append(f'{target.name}的【證據保全】擋下本次反駁。')
        return max(0, removed - 1)

    def _add_evidence(self, actor, kind, fraction, name):
        key = self.add_object(actor, kind, fraction)
        evidence = self.fighter_for_key(key)
        evidence.name = name
        evidence.stats['防禦'] = 0
        evidence.status_stacks['rest_evidence'] = 1
        return key

    def _start_ema_guilt(self, actor):
        players = sorted(self.living(0), key=lambda player: (player.hp / player.stats['HP'], player.user_id))
        count = 2 if self.enrage >= 4_000 and len(players) >= 4 else 1
        defendants = players[:count]
        advocates = [player for player in players if player not in defendants]
        objects, assignments = [], {}
        for index, defendant in enumerate(defendants):
            key = self._add_evidence(actor, 'guilt_evidence', .01, '斷罪證物')
            evidence = self.fighter_for_key(key)
            evidence.hp = evidence.stats['HP'] = 1
            evidence.status_stacks['rest_guilt_evidence'] = 1
            objects.append(key)
            assignments[key] = [player.user_id for offset, player in enumerate(advocates)
                                if offset % count == index]
        self.pending['ema'] = dict(
            due=self.round + 1, phase=0, kind='rest_guilt',
            targets=[self.key(player) for player in defendants], objects=objects,
            assignments=assignments, hits={}, defended=[])
        self.mechanics['ema_guilt_due'] = False
        self.mechanics['ema_guilt_used'] = True
        self.log.append('【有罪推定】已提出；被告必須防禦，辯護人必須對斷罪證物舉證。')

    def _start_ema_final(self, actor):
        if 'ema' in self.pending:
            self.cancel_spell('ema')
        shadows = {}
        for player in self.living(0):
            player.status_stacks['factor'] = 3
            player.effects['factor'] = self.round + 30
            key = self._add_evidence(actor, 'factor_shadow', .01, f'{player.name}的因子殘影')
            shadow = self.fighter_for_key(key)
            shadow.hp = shadow.stats['HP'] = 1
            shadows[key] = player.user_id
        self.mechanics['ema_final'] = dict(
            active=True, start_round=self.round, step=0,
            manifests=[player.user_id for player in self.living(0)], shadows=shadows,
            denied=[], advocated=[], shield_gained=0)
        self.mechanics['ema_final_next'] = self.round + (5 if self.enrage >= 4_000 else 6)
        self.log.append('【最終判決・魔女殺手】開始；顯現魔女必須本人防禦，並由另一人攻擊其因子殘影。')

    def _hiro_reference(self, length):
        history = self.mechanics.get('rest_action_history', {})
        return {str(player.user_id): list(history.get(str(player.user_id), []))[-length:]
                for player in self.living(0)}

    def _hiro_rewind(self, actor, number):
        self.mechanics['hiro_rewinds'] = number
        percent = (30 if number == 1 else
                   25 if self.enrage >= 4_000 else 20 if self.enrage >= 2_000 else
                   15 if self.enrage >= 1_000 else 5)
        actor.hp = max(1, actor.stats['HP'] * percent // 100)
        actor.effects.clear()
        if number == 1:
            self.mechanics['hiro_dislocation_until'] = self.round + 1
        replay = number == 1 or self.enrage >= 1_000
        if replay:
            self.mechanics['hiro_compare'] = dict(
                active=True, start_round=self.round, remaining=2,
                reference=self._hiro_reference(2))
            if self.enrage >= 500:
                attacks = list(self.mechanics.get('hiro_attacks', []))[-2:]
                echoes = []
                for index, attack in enumerate(attacks):
                    fraction = .12 if self.enrage >= 4_000 else .09 if self.enrage >= 2_000 else .07 if self.enrage >= 1_000 else .05
                    key = self._add_evidence(actor, 'memory_fragment', fraction, '記憶斷片')
                    due = self.round + (1 if self.enrage >= 4_000 else index + 1)
                    echoes.append(dict(object=key, due=due, target=attack['target'], power=attack['power']))
                self.mechanics['hiro_echoes'] = echoes
        if number == 2 and self.enrage >= 1_000:
            actor.hp = 1
            for player in self.living(0):
                ready = [rule.slot for rule in player.rules if rule.enabled]
                if ready:
                    slot = min(ready, key=lambda value: player.ready.get(value, 0))
                    player.ready[slot] = self.round
            players = sorted(self.living(0), key=lambda item: item.user_id)
            focus_count = 0 if self.enrage < 2_000 else 1 if self.enrage < 4_000 or len(players) == 3 else 2
            self.mechanics['hiro_final'] = dict(
                active=True, start_round=self.round, step=0,
                reference=self._hiro_reference(3), rewrite_used=[], doomed=[], failed_rounds=[],
                previous={}, focus=[item.user_id for item in players[:focus_count]])
        self.log.append(f'{actor.name}發動第 {number} 次【死亡回溯】，以 {percent}% HP 返回戰場。')

    def act(self, actor):
        if actor.job == 'ema':
            final = self.mechanics.get('ema_final')
            if final and final.get('active'):
                self.log.append(f'{actor.name}正在執行【最終判決・魔女殺手】。')
                return
            if (750 <= self.enrage < 1_000 and not self.mechanics.get('ema_guilt_used')
                    and actor.hp * 100 <= actor.stats['HP'] * 35):
                self.mechanics['ema_guilt_due'] = True
            final_due = (self.enrage >= 1_000 and actor.hp * 100 <= actor.stats['HP'] * 35
                         and (not final or not final.get('active'))
                         and self.round >= self.mechanics.get('ema_final_next', 0))
            if final_due:
                self._start_ema_final(actor)
                return
            if self.mechanics.get('ema_guilt_due'):
                self._start_ema_guilt(actor)
                return
        if actor.job == 'hiro':
            if self.mechanics.get('hiro_final', {}).get('active'):
                self.log.append(f'{actor.name}在最後一條世界線中維持 1 HP，本回合不攻擊。')
                return
            before = len(self.mechanics.get('hiro_attacks', []))
            result = super().act(actor)
            if actor.hp > 0 and actor.hp * 100 <= actor.stats['HP'] * 35 and len(self.living(0)):
                target = self.victim(actor)
                if target:
                    self.hit(actor, target, .6)
            if len(self.mechanics.get('hiro_attacks', [])) == before:
                self.mechanics.setdefault('hiro_attacks', [])
            return result
        result = super().act(actor)
        if actor.job == 'ema' and self.enrage >= 500:
            for player in self.living(0):
                stacks = self.factor_stacks(player)
                if stacks == 3 or self.enrage >= 4_000 and stacks:
                    if player.status_stacks.get('rest_factor_preservation', 0) < 1:
                        player.status_stacks['rest_factor_preservation'] = 1
        return result

    def prepare(self, actor, kind=None):
        if actor.job != 'ema' or kind:
            return super().prepare(actor, kind)
        candidates = [player for player in self.living(0) if self.factor_stacks(player)]
        candidates.sort(key=lambda player: (-self.factor_stacks(player),
                                             player.hp / player.stats['HP'], player.user_id))
        count = 1
        if self.enrage >= 250 and actor.hp * 100 <= actor.stats['HP'] * 70:
            count = 2
        if self.enrage >= 2_000:
            count = 3
        if self.enrage >= 4_000:
            count = len(candidates)
        objects = []
        if self.enrage >= 500:
            fraction = .12 if self.enrage >= 4_000 else .10 if self.enrage >= 2_000 else .08 if self.enrage >= 1_000 else .06
            key = self.add_object(actor, 'factor_evidence', fraction)
            evidence = self.fighter_for_key(key)
            evidence.name = '因子證物'
            evidence.stats['防禦'] = 0
            evidence.status_stacks['rest_evidence'] = 1
            objects.append(key)
        self.pending['ema'] = dict(due=self.round + 1, phase=0,
                                   kind='rest_prosecution',
                                   targets=[self.key(player) for player in candidates[:count]],
                                   objects=objects,
                                   objection_resist=2 if self.enrage >= 4_000 else 1 if self.enrage >= 500 else 0)
        self.next_cast['ema'] = self.round + self.rng.randint(3, 4)
        self.events['telegraphs'] += 1
        names = '、'.join(player.name for player in candidates[:count]) or '無有效被告'
        self.log.append(f'【{actor.name}】提出起訴，鎖定：{names}；下回合作出判決。')
        if objects:
            self.log.append(f'【因子證物】出現；判決前擊破可論破本次起訴。')

    def spell(self, actor, data):
        if actor.job == 'ema' and data.get('kind') == 'rest_guilt':
            targets = [self.fighter_for_key(key) for key in data['targets']]
            for index, target in enumerate(targets):
                if not target or target.hp <= 0:
                    continue
                object_key = data['objects'][index]
                required = (set(data['assignments'][object_key]) if self.enrage >= 1_000
                            else set(data['assignments'][object_key][:2]))
                hits = set(data['hits'].get(object_key, []))
                success = target.user_id in data['defended'] and required <= hits
                if success:
                    percent = 70 if self.enrage >= 2_000 else 60 if self.enrage >= 1_000 else 40
                    damage = int(target.stats['HP'] * percent / 100
                                 * self.damage_taken_multiplier(target))
                    actual, _, shared = self.apply_damage(
                        target, damage, direct=True)
                    self.log.append(f'{target.name}已被論破，承受 {actual + shared} 點判決傷害。')
                elif self.enrage >= 1_000:
                    target.hp = 0
                    self.log.append(f'{target.name}未完成有罪推定，遭處刑即死。')
                else:
                    damage = int(target.stats['HP'] * self.damage_taken_multiplier(target))
                    actual, _, shared = self.apply_damage(target, damage, direct=True)
                    self.log.append(f'{target.name}的舉證不足，承受 {actual + shared} 點有罪判決傷害。')
            for key in data['objects']:
                self.fighter_for_key(key).hp = 0
            return
        if actor.job != 'ema' or data.get('kind') != 'rest_prosecution':
            return super().spell(actor, data)
        if data['objects'] and not any(self.fighter_for_key(key).hp > 0 for key in data['objects']):
            self.events['objects_broken'] += 1
            self.log.append('因子證物已被擊破，起訴不成立。')
            return
        targets = [self.fighter_for_key(key) for key in data['targets']]
        targets = [target for target in targets if target and target.hp > 0]
        if not targets:
            self.log.append(f'{actor.name}的起訴沒有有效被告，本次撤銷。')
            return
        consumed = 0
        for target in targets:
            stacks = self.factor_stacks(target)
            if not stacks:
                self.log.append(f'{target.name}已反駁所有魔女因子，判決取消。')
                continue
            requested = int(target.stats['HP'] * (12 + stacks * 8) / 100
                            * self.damage_taken_multiplier(target))
            actual, _, shared = self.apply_damage(target, requested, direct=True)
            target.status_stacks.pop('factor', None)
            target.effects.pop('factor', None)
            consumed += stacks
            self.log.append(
                f'{actor.name}對 {target.name}作出判決，消耗 {stacks} 層魔女因子，造成 {actual + shared} 傷害。')
        if consumed:
            healing = self.restore(actor, actor.stats['HP'] * consumed // 200)
            shield = actor.stats['HP'] * consumed // 100
            self.mechanics['rest_boss_shield'] = self.mechanics.get('rest_boss_shield', 0) + shield
            self.log.append(f'{actor.name}回復 {healing} HP，並取得 {shield} 點判決護盾。')
        for key in data['objects']:
            self.fighter_for_key(key).hp = 0
        count = self.mechanics.get('ema_prosecutions', 0) + 1
        self.mechanics['ema_prosecutions'] = count
        if (self.enrage >= 4_000 or self.enrage >= 1_000 and count % 2 == 0):
            self.mechanics['ema_guilt_due'] = True

    def _resolve_player(self, actor, choice):
        category = self.action_category(choice)
        old_history = self.mechanics.setdefault('rest_action_history', {}).setdefault(
            str(actor.user_id), [])
        previous = old_history[-1] if old_history else None
        target_key = choice.target
        data = self.pending.get('ema')
        if data and data.get('kind') == 'rest_guilt':
            if self.key(actor) in data['targets'] and category == 'defend':
                data['defended'].append(actor.user_id)
            if target_key in data['objects'] and category in ('attack', 'skill'):
                allowed = data['assignments'].get(target_key, [])
                if actor.user_id in allowed:
                    data['hits'].setdefault(target_key, []).append(actor.user_id)
        ema_final = self.mechanics.get('ema_final')
        if ema_final and ema_final.get('active'):
            if category == 'defend':
                ema_final['denied'].append(actor.user_id)
            owner = ema_final['shadows'].get(target_key)
            if owner is not None and owner != actor.user_id and category in ('attack', 'skill'):
                ema_final['advocated'].append(owner)
        hiro_final = self.mechanics.get('hiro_final')
        rewrite = choice.action in REWRITE_ACTIONS
        if hiro_final and hiro_final.get('active'):
            hiro_final.setdefault('current', {})[str(actor.user_id)] = category
            if rewrite:
                hiro_final['rewrite_used'].append(actor.user_id)
                hiro_final.setdefault('rewritten_this_round', []).append(actor.user_id)
        self._rest_player_actor = actor
        self._rest_target_key = target_key
        self._rest_testimony_triggered = False
        try:
            if rewrite:
                self.log.append(f'{actor.name}使用【{REWRITE_ACTIONS[choice.action][0]}】。')
            else:
                super()._resolve_player(actor, choice)
        finally:
            self._rest_player_actor = None
            self._rest_target_key = None
        old_history.append(category)
        del old_history[:-12]
        boss = self.witch('hiro')
        if (boss and boss.hp > 0 and boss.hp * 100 <= boss.stats['HP'] * 70
                and previous == category and actor.hp > 0
                and not self.mechanics.get('hiro_final', {}).get('active')):
            self.hit(boss, actor, .7)
            self.log.append(f'{boss.name}的【記憶追擊】命中重複行動的 {actor.name}。')

    def _resolve_ema_final_round(self):
        final = self.mechanics.get('ema_final')
        if not final or not final.get('active') or self.round <= final['start_round']:
            return
        paired = set(final['denied']) & set(final['advocated']) & set(final['manifests'])
        for user_id in paired:
            final['manifests'].remove(user_id)
            player = next((item for item in self.fighters if item.user_id == user_id), None)
            if player:
                player.status_stacks.pop('factor', None)
                player.effects.pop('factor', None)
            for key, owner in final['shadows'].items():
                if owner == user_id:
                    self.fighter_for_key(key).hp = 0
            self.log.append(f'<@{user_id}> 完成否認與舉證，已不再是顯現魔女。')
        drained = 0
        percent = 18 if self.enrage >= 4_000 else 15 if self.enrage >= 2_000 else 12
        for user_id in list(final['manifests']):
            player = next((item for item in self.living(0) if item.user_id == user_id), None)
            if player:
                amount = min(player.hp, player.stats['HP'] * percent // 100)
                player.hp -= amount
                drained += amount
        boss = self.witch('ema')
        self.mechanics['rest_boss_shield'] = self.mechanics.get('rest_boss_shield', 0) + drained
        final['shield_gained'] += drained
        final['step'] += 1
        final['denied'], final['advocated'] = [], []
        if final['step'] < 3:
            return
        if final['manifests']:
            for user_id in final['manifests']:
                player = next((item for item in self.fighters if item.user_id == user_id), None)
                if player and player.hp > 0:
                    player.hp = 0
                    self.log.append(f'{player.name}被【魔女殺手】判定為魔女，即死。')
        else:
            self.mechanics['rest_boss_shield'] = max(
                0, self.mechanics.get('rest_boss_shield', 0) - final['shield_gained'])
            self.mechanics['rest_vulnerable_until'] = self.round + 1
            self.log.append('全員完成反駁，魔女殺手失敗；艾瑪進入證言崩潰。')
        final['active'] = False

    def _resolve_hiro_round(self):
        boss = self.witch('hiro')
        compare = self.mechanics.get('hiro_compare')
        if compare and compare.get('active') and self.round > compare['start_round']:
            index = 2 - compare['remaining']
            percent = 45 if self.enrage >= 4_000 else 35 if self.enrage >= 2_000 else 30 if self.enrage >= 1_000 else 25
            history = self.mechanics.get('rest_action_history', {})
            for player in list(self.living(0)):
                reference = compare['reference'].get(str(player.user_id), [])
                current = history.get(str(player.user_id), [])[-1:]
                if index < len(reference) and current and current[0] == reference[index]:
                    damage = int(player.stats['HP'] * percent / 100
                                 * self.damage_taken_multiplier(player))
                    actual, _, shared = self.apply_damage(player, damage, direct=True)
                    self.log.append(f'{boss.name}對被看穿的 {player.name}反擊 {actual + shared} 傷害。')
            compare['remaining'] -= 1
            compare['active'] = compare['remaining'] > 0
        echoes = self.mechanics.get('hiro_echoes', [])
        for echo in [item for item in echoes if item['due'] <= self.round]:
            fragment = self.fighter_for_key(echo['object'])
            if fragment.hp > 0 and boss and boss.hp > 0:
                target = self.fighter_for_key(echo['target'])
                if not target or target.hp <= 0:
                    target = min(self.living(0), key=lambda item: item.user_id, default=None)
                if target:
                    power = echo['power'] * (1.2 if self.enrage >= 2_000 else 1)
                    self.hit(boss, target, power)
                    self.log.append(f'{boss.name}的【歷史殘響】向 {target.name}重演。')
            else:
                self.log.append('記憶斷片已被擊破，對應的歷史重演取消。')
            fragment.hp = 0
            echoes.remove(echo)
        final = self.mechanics.get('hiro_final')
        if not final or not final.get('active') or self.round <= final['start_round']:
            return
        step = final['step']
        current = final.get('current', {})
        categories = set(current.values())
        if not {'attack', 'skill', 'defend'} <= categories:
            final['failed_rounds'].append(step + 1)
        rewritten = set(final.get('rewritten_this_round', []))
        for player in self.living(0):
            user_key = str(player.user_id)
            category = current.get(user_key)
            reference = final['reference'].get(user_key, [])
            if player.user_id not in rewritten and step < len(reference) and category == reference[step]:
                final['doomed'].append(player.user_id)
            if (player.user_id in final.get('focus', []) and step > 0
                    and category == final['previous'].get(user_key)):
                final['doomed'].append(player.user_id)
            final['previous'][user_key] = category
        final['step'] += 1
        final['current'], final['rewritten_this_round'] = {}, []
        players = sorted(self.living(0), key=lambda item: item.user_id)
        focus_count = 0 if self.enrage < 2_000 else 1 if self.enrage < 4_000 or len(players) == 3 else 2
        final['focus'] = [item.user_id for item in players[final['step']:final['step'] + focus_count]]
        if final['step'] < 3:
            return
        if final['failed_rounds']:
            for player in self.living(0):
                player.hp = 0
            self.log.append('因果分歧未完成，全隊遭最後一條世界線即死判定。')
        else:
            for user_id in set(final['doomed']):
                player = next((item for item in self.fighters if item.user_id == user_id), None)
                if player and player.hp > 0:
                    player.hp = 0
                    self.log.append(f'{player.name}的歷史重複，遭既定死亡即死判定。')
            if self.living(0):
                final['active'] = False
                self.mechanics['rest_vulnerable_until'] = self.round + 1
                self.log.append('隊伍脫離已知歷史，希羅進入未知未來。')

    def resolve(self, use_defaults=False):
        result = super().resolve(use_defaults)
        if result and result != '平手（達回合上限）':
            return result
        if self.witch_id == 'ema':
            self._resolve_ema_final_round()
        else:
            self._resolve_hiro_round()
        if self.result == '平手（達回合上限）':
            return self.result
        self.check_end()
        return self.result

    def intent(self):
        if self.witch_id == 'hiro':
            final = self.mechanics.get('hiro_final')
            if final and final.get('active'):
                step = final['step']
                lines = [f'第 {step + 1}/3 回合：全隊需同時出現普攻、主動技能、防禦。']
                for player in self.living(0):
                    ref = final['reference'].get(str(player.user_id), [])
                    expected = ref[step] if step < len(ref) else '無紀錄'
                    focus = '｜記憶焦點' if player.user_id in final.get('focus', []) else ''
                    lines.append(f'{player.name}：不可重複 {expected}{focus}')
                return EnemyIntent(self.planning_round, '最後一條世界線', '\n'.join(lines))
            notes = []
            compare = self.mechanics.get('hiro_compare')
            if compare and compare.get('active'):
                notes.append(f'既視反擊剩餘 {compare["remaining"]} 回合')
            echoes = self.mechanics.get('hiro_echoes', [])
            for echo in echoes:
                fragment = self.fighter_for_key(echo['object'])
                target = self.fighter_for_key(echo['target'])
                notes.append(f'歷史殘響：第 {echo["due"]} 回合指向 '
                             f'{target.name if target else "順位候補"}｜斷片 {fragment.hp:,}/{fragment.stats["HP"]:,}')
            if notes:
                return EnemyIntent(self.planning_round, '死亡回溯・公開歷史', '\n'.join(notes))
            return super().intent()
        final = self.mechanics.get('ema_final')
        if final and final.get('active'):
            lines = [f'第 {final["step"] + 1}/3 回合｜生氣吸取後施放魔女殺手']
            for user_id in final['manifests']:
                player = next((item for item in self.fighters if item.user_id == user_id), None)
                lines.append(f'{player.name}：'
                             f'{"已否認" if user_id in final["denied"] else "尚未否認"}｜'
                             f'{"已舉證" if user_id in final["advocated"] else "尚未舉證"}')
            return EnemyIntent(self.planning_round, '最終判決・魔女殺手', '\n'.join(lines))
        data = self.pending.get('ema')
        if data and data.get('kind') == 'rest_guilt':
            lines = []
            for index, key in enumerate(data['targets']):
                target = self.fighter_for_key(key)
                object_key = data['objects'][index]
                required = data['assignments'][object_key]
                hits = data['hits'].get(object_key, [])
                lines.append(f'{target.name}：{"已防禦" if target.user_id in data["defended"] else "尚未防禦"}｜'
                             f'舉證 {len(set(hits))}/{len(required) if self.enrage >= 1_000 else min(2, len(required))}')
            return EnemyIntent(self.planning_round, '有罪推定', '\n'.join(lines))
        if data and data.get('kind') == 'rest_prosecution':
            targets = [self.fighter_for_key(key) for key in data['targets']]
            details = '、'.join(
                f'{target.name}（{self.factor_stacks(target)} 層）'
                for target in targets if target) or '無有效被告'
            counters = []
            if data.get('objection_resist'):
                counters.append(f'異議棄卻 {data["objection_resist"]}')
            if data['objects']:
                evidence = self.fighter_for_key(data['objects'][0])
                counters.append(f'因子證物 HP {evidence.hp:,}/{evidence.stats["HP"]:,}')
            suffix = ('\n' + '｜'.join(counters)) if counters else ''
            return EnemyIntent(self.planning_round, '起訴・魔女因子',
                               f'下回合判決：{details}。反駁因子或擊破證物可使判決取消。{suffix}')
        return EnemyIntent(self.planning_round, '魔女因子',
                           '普通攻擊命中會施加 1 層魔女因子；最高 3 層。')


class WitchRestAutoBattle(Battle):
    def __init__(self, fighters, witch_id, seed=None):
        super().__init__(fighters, seed=seed, max_rounds=30)
        self.witch_id = witch_id
        self.mechanics['witch_rest_mode'] = 'auto'
        self.mechanics['witch_id'] = witch_id
        self.mechanics['hiro_rewound'] = False

    def check_end(self):
        boss = next((fighter for fighter in self.fighters if fighter.team == 1), None)
        if (self.witch_id == 'hiro' and boss and boss.hp <= 0
                and not self.mechanics['hiro_rewound']):
            self.mechanics['hiro_rewound'] = True
            boss.hp = max(1, boss.stats['HP'] * 15 // 100)
            boss.effects.clear()
            self.log.append(f'{boss.name} 發動【弱化死亡回溯】，以 15% HP 返回戰場。')
        return super().check_end()

    def act(self, actor):
        if actor.team == 0:
            return super().act(actor)
        target = self.target(actor, self.living(0), Rule(0, 0, True, 'always', 'lowest'), True)
        if target is None:
            return
        self.record_skill(actor, '普通攻擊')
        self.log.append(f'{actor.name} 使用普通攻擊')
        hit = self.basic_attack(actor, target)
        if self.witch_id != 'ema':
            return
        factors = self.mechanics.setdefault('ema_factors', {})
        if hit and target.hp > 0:
            key = str(target.user_id)
            factors[key] = min(3, factors.get(key, 0) + 1)
            self.log.append(f'{target.name} 的魔女因子增加至 {factors[key]}/3 層。')
        if self.round % 4:
            return
        living = [fighter for fighter in self.living(0) if factors.get(str(fighter.user_id), 0)]
        if not living:
            return
        victim = max(living, key=lambda fighter: (factors[str(fighter.user_id)], -fighter.hp))
        stacks = factors.pop(str(victim.user_id))
        damage = victim.stats['HP'] * (10 + stacks * 6) // 100
        actual, _, shared = self.apply_damage(victim, damage, direct=True)
        self.log.append(f'{actor.name} 引爆 {victim.name} 的 {stacks} 層魔女因子，造成 {actual + shared} 傷害。')


def auto_battle_from_participants(participants, witch_id, enrage=0, seed=None):
    if witch_id not in WITCHES or not participants:
        raise CharacterError('無效的魔女安息儀式戰鬥。')
    players, _, _, = participant_fighters(participants)
    hp, attack, defense = PANEL_STATS[witch_id]
    size = len(participants)
    attack_scale = {1: .85, 2: .90, 3: .95, 4: 1, 5: 1.05, 6: 1.10}[size]
    stats = {'HP': max(1, round(hp * size / 4)), '攻擊': round(attack * attack_scale),
             '防禦': defense, '治療量': 0, '命中率': 100, '閃避率': 5, '暴擊率': 10}
    boss = Fighter(WITCHES[witch_id].name, 1, f'witch_rest:{witch_id}', stats, 50, [], is_boss=True)
    battle = WitchRestAutoBattle(players + [boss], witch_id, seed)
    battle.mechanics.update(party_size=size, enrage=enrage)
    return battle


def run_auto_battle(participants, witch_id, enrage=0, seed=None):
    battle = auto_battle_from_participants(participants, witch_id, enrage, seed)
    while not battle.result:
        battle.step()
    return battle


def manual_battle_from_participants(participants, witch_id, enrage, seed=None):
    if witch_id not in WITCHES or not 100 <= enrage <= 4_000 or not 3 <= len(participants) <= 6:
        raise CharacterError('無效的魔女安息儀式手動戰鬥。')
    snapshot = raid_battle(
        participants, {'kind': '魔女安息儀式參戰資料', 'name': '魔女安息儀式參戰資料', 'strength': 1.0}, seed)
    players = [fighter for fighter in snapshot.fighters if fighter.team == 0]
    hp, attack, defense = manual_stats(witch_id, enrage)
    size = len(participants)
    attack_scale = {3: .95, 4: 1, 5: 1.05, 6: 1.10}[size]
    stats = {'HP': round(hp * size / 4), '攻擊': round(attack * attack_scale),
             '防禦': defense, '治療量': 0, '命中率': 100, '閃避率': 5, '暴擊率': 10}
    boss = Fighter(WITCHES[witch_id].name, 1, witch_id, stats, 50, [], is_boss=True)
    battle = WitchRestManualBattle(players + [boss], witch_id, enrage, seed)
    battle.mechanics.update(witch_party_size=size, witch_stat_anchor=manual_stats(witch_id, enrage))
    battle.log.extend(snapshot.log)
    return battle
