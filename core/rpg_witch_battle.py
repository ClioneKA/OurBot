"""Manual, restartable daily witch raid using real character snapshots."""
from collections import Counter
from dataclasses import asdict, replace

from core.rpg_battle import ALLY_EFFECTS, FIXED_TARGETS, Fighter, dump_battle, load_battle, raid_battle
from core.rpg_total_battle import ActionChoice, EnemyIntent, TotalRaidBattle, TotalRaidError, ACTION_ATTACK, ACTION_SKILL
from core.rpg_witch_catalog import IDS, PROFILE
from core.rpg_witch_mechanics import WitchBattleV9, SUPPORT
from core.rpg_witch_scaling import SCALING_VERSION, witch_stat_scales

ACTION_DEFEND = 'defend'
STATE_FIELDS = '''ids order pending next_cast link_next active_link reactions reacted stun_next objects
history current_kind recorded_hp animal_bag incoming hits_by_type previous_damage_type adaptation
link_protected ema_followup special_power last_killer dead_once rewound events link pacing tuning
changed command_base command_locked final_command_turns commands prayer_shields recovery
brainwash_aware forced_brainwash confirmed auto_players timeout_streak'''.split()
OBJECT_NAMES = {'painting': '攻擊畫作', 'animal_painting': '動物召喚畫', 'rock': '落石',
                'boulder': '連動巨石', 'rabbit': '噴火的兔子', 'snake': '莊嚴的白蛇', 'bird': '利爪的飛鳥'}
SPELL_DESCRIPTIONS = {
    'ema': ('引爆最多魔女因子的目標；可淨化魔女因子', '引爆最多魔女因子的目標', '全體魔女因子引爆'),
    'hiro': ('單體打擊；保留一次回溯', '單體打擊；強化回溯', '單體兩段打擊'),
    'anan': ('洗腦 1 人：攻擊／治療反轉', '洗腦 2 人：強制普攻隊友', '洗腦 4 人：強制普攻隊友'),
    'noah': ('畫作單體打擊', '畫作單體打擊及追擊', '單體與全體畫作'),
    'reia': ('聚光承接單體攻擊', '聚光承接單體攻擊並減傷', '分兩回合攻擊兩批玩家'),
    'milia': ('交換兩人 HP 比例；可淨化標記', '交換 HP 比例與指定增減益', '適應主要傷害類別'),
    'margo': ('懷疑削弱輸出與治療；防禦可避開', '單體懷疑；防禦可避開', '全體懷疑；防禦可避開'),
    'nanoka': ('預知一人；避免重複行動', '預知兩人；重複行動遭追擊', '全體預知；重複行動遭追擊'),
    'arisa': ('單體火傷', '火傷擴散；可淨化來源', '全體引爆；火傷者承受更多傷害'),
    'sherry': ('單體重擊', '破防連擊', '全體重擊及單體追擊'),
    'hanna': ('落石全體攻擊', '落石全體攻擊', '三塊落石分別攻擊全體'),
    'coco': ('單體監視打擊', '兩人監視打擊', '全體打擊；被監視者承受更多傷害'),
    'meruru': ('預告救援 18% HP，溢補轉盾', '預告救援 20% HP、淨化與護盾', '預告救援 22% HP、淨化與護盾'),
}
WITCH_TRAITS = {
    'ema': '普攻命中累積魔女因子（最多 3 層）；可淨化。有人達 2 層時，即將就緒的魔法可提前一回合準備。',
    'hiro': '每場僅一次倒下回溯；二次魔女化時無法發動。',
    'anan': '洗腦不可淨化；打斷或擊倒安安可解除，至少保留一位存活玩家不受洗腦。',
    'noah': '拆除畫作可阻止相應攻擊。',
    'reia': '聚光結束後遭破甲。',
    'milia': '交換／適應魔法後，追加一次預告的普通攻擊。',
    'margo': '懷疑使傷害與治療降低 30%；魔法後追加一次預告的普通攻擊。',
    'nanoka': '預知下重複普攻或同名技能，本次攻擊力降低 40%。',
    'arisa': '火傷於回合結束扣除最大 HP 的 3%。二次魔女化且至少兩人火傷時，即將就緒的魔法可提前一回合準備。',
    'sherry': '二次魔女化的重擊後遭破甲，下一回合進入恢復。',
    'hanna': '奇數回合或浮游期間，承受裝甲步兵／騎士的非必中攻擊減傷 30%；落石可拆除。',
    'coco': '普攻命中施加監視標記。至少兩人被監視時，即將就緒的魔法可提前一回合準備。',
    'meruru': '溢出治療轉為護盾，上限為救援對象最大 HP 的 12%。有魔女低於半血時，即將就緒的救援可提前一回合準備。',
}
# Opening preparation windows and later preparation intervals. Every cast still
# resolves one turn after its announcement; randomness uses the saved battle RNG.
WITCH_RHYTHMS = {
    'ema': ((2, 3), (3, 4)), 'hiro': ((1, 2), (3, 4)),
    'anan': ((1, 2), (3, 4)), 'noah': ((2, 3), (3, 4)),
    'reia': ((1, 2), (3, 4)), 'milia': ((2, 3), (4, 5)),
    'margo': ((1, 2), (3, 4)), 'nanoka': ((1, 2), (3, 4)),
    'arisa': ((1, 3), (3, 4)), 'sherry': ((2, 3), (4, 5)),
    'hanna': ((1, 3), (3, 5)), 'coco': ((2, 3), (3, 4)),
    'meruru': ((2, 3), (3, 4)),
}


class WitchRaidBattle(WitchBattleV9):
    def __init__(self, fighters, ids, seed=None, max_rounds=30):
        if len(ids) != 3 or len(set(ids)) != 3 or any(k not in IDS for k in ids):
            raise TotalRaidError('每日總力戰需要三位不同魔女。')
        super().__init__(fighters, ids, seed=seed, max_rounds=max_rounds, tuning=True)
        self.next_cast = {key: self.rng.randint(*WITCH_RHYTHMS[key][0]) for key in ids}
        if len(set(self.next_cast.values())) == 1:
            key = self.rng.choice(list(ids))
            low, high = WITCH_RHYTHMS[key][0]
            self.next_cast[key] = low if self.next_cast[key] != low else high
        self.confirmed = set()
        self.auto_players = set()
        self.timeout_streak = {}

    def available_actions(self, user_id):
        actor = self._player(user_id)
        actions = TotalRaidBattle.available_actions(self, user_id)
        if self.forced(actor, self.planning_round):
            return [dict(action=ACTION_ATTACK, name='洗腦・強制普攻隊友', skill_slot=None,
                         description='只能普攻另一位存活隊友；安安被打斷或倒下可解除。')]
        actions.append(dict(action=ACTION_DEFEND, name='防禦', skill_slot=None,
                            description='本回合承受傷害減半。'))
        if self.washed(actor, self.planning_round):
            for item in actions:
                if item['action'] == ACTION_ATTACK:
                    item['description'] = '洗腦中：攻擊另一位存活隊友。'
                elif item['action'] == ACTION_SKILL:
                    effect = self._skill(actor, item['skill_slot'])[1].effect
                    if effect in ('heal', 'group_heal', 'rally', 'holy_light'):
                        item['description'] = '洗腦中：治療會給魔女，傷害會打隊友。'
                    elif effect not in SUPPORT:
                        item['description'] = '洗腦中：傷害與附帶效果轉向隊友。'
        return actions

    def valid_targets(self, user_id, action, skill_slot=None):
        actor = self._player(user_id)
        if action == ACTION_DEFEND:
            return []
        if action not in (ACTION_ATTACK, ACTION_SKILL):
            raise TotalRaidError('請選擇普通攻擊、技能或防禦。')
        effect = self._skill(actor, skill_slot)[1].effect if action == ACTION_SKILL else action
        if self.washed(actor, self.planning_round):
            if effect in ('group_heal', 'rally', 'area', 'cleave'):
                return []
            if effect in ('heal', 'holy_light'):
                return [self.key(w) for w in self.witches()]
            if effect not in SUPPORT:
                return [self.key(p) for p in self.living(0) if p is not actor]
        targets = TotalRaidBattle.valid_targets(self, user_id, action, skill_slot)
        if actor.has('no_look', self.planning_round):
            targets = [k for k in targets if self.fighter_for_key(k).job != 'animal_painting']
        return targets

    def submit(self, user_id, action, target=None, skill_slot=None):
        actor = self._player(user_id)
        if self.result or actor.hp <= 0:
            raise TotalRaidError('戰鬥已結束或你已倒下。')
        allowed = [a for a in self.available_actions(user_id) if a['action'] == action
                   and (action != ACTION_SKILL or a['skill_slot'] == skill_slot)]
        if not allowed:
            raise TotalRaidError('目前無法使用此行動；洗腦強化時只能普攻隊友。')
        if allowed[0].get('cooldown_remaining', 0):
            raise TotalRaidError('技能仍在冷卻中。')
        targets = self.valid_targets(user_id, action, skill_slot)
        if targets and target not in targets:
            raise TotalRaidError('請選擇有效的行動目標。')
        choice = ActionChoice(user_id, action, target if targets else None,
                              skill_slot if action == ACTION_SKILL else None)
        self.choices[user_id] = choice
        self.confirmed.discard(user_id)
        self.auto_players.discard(user_id)
        self.timeout_streak[user_id] = 0
        return choice

    def confirm(self, user_id):
        if user_id not in self.living_player_ids() or user_id not in self.choices:
            raise TotalRaidError('請先選擇本回合行動。')
        self.confirmed.add(user_id)

    def enable_auto(self, user_id):
        actor = self._player(user_id)
        if self.result or actor.hp <= 0:
            raise TotalRaidError('戰鬥已結束或你已倒下。')
        self.auto_players.add(user_id)
        self.confirmed.discard(user_id)
        self.choices.pop(user_id, None)
        self.timeout_streak[user_id] = 0

    def takeover(self, user_id):
        self._player(user_id)
        self.auto_players.discard(user_id)
        self.timeout_streak[user_id] = 0
        self.confirmed.discard(user_id)
        if self.choices.get(user_id) and self.choices[user_id].automatic:
            self.choices.pop(user_id)

    def waiting_player_ids(self):
        return self.living_player_ids() - self.confirmed - self.auto_players

    def fill_defaults(self):
        added = []
        for uid in sorted(self.living_player_ids()):
            if uid in self.confirmed and uid in self.choices:
                continue
            action, slot = ACTION_ATTACK, None
            if uid in self.auto_players:
                actor = self._player(uid)
                enabled = {rule.slot for rule in actor.rules if rule.enabled}
                skills = sorted((item for item in self.available_actions(uid)
                                 if item['action'] == ACTION_SKILL
                                 and item['skill_slot'] in enabled
                                 and not item.get('cooldown_remaining', 0)),
                                key=lambda item: item['skill_slot'])
                if skills:
                    action, slot = ACTION_SKILL, skills[0]['skill_slot']
            targets = self.valid_targets(uid, action, slot)
            target = min(targets, key=lambda k: self.fighter_for_key(k).hp
                         / self.fighter_for_key(k).stats['HP']) if targets else None
            choice = ActionChoice(uid, action, target, slot, automatic=True)
            self.choices[uid] = choice
            added.append(choice)
        return added

    def resolve(self, use_defaults=False):
        if self.result or self.check_end():
            return self.result
        if self.waiting_player_ids() and not use_defaults:
            raise TotalRaidError('尚有玩家未確認行動。')
        if use_defaults:
            for uid in self.waiting_player_ids():
                self.timeout_streak[uid] = self.timeout_streak.get(uid, 0) + 1
                if self.timeout_streak[uid] >= 3:
                    self.auto_players.add(uid)
        self.fill_defaults()
        if 'witch_round_logs' not in self.mechanics:
            self.mechanics['witch_initial_log'] = list(self.log)
        start = len(self.log)
        self.log.append(f'── 第 {self.planning_round} 回合 ──')
        result = super().resolve(use_defaults=False)
        self.confirmed.clear()
        self.mechanics['last_round_log'] = self.log[start:]
        self.mechanics.setdefault('witch_round_logs', []).append(self.log[start:])
        # Keep durable snapshots bounded even during long raids.
        self.log = self.log[-300:]
        return result

    def add_object(self, owner, kind, hp_fraction, power=0, group=False):
        key = super().add_object(owner, kind, hp_fraction, power, group)
        self.fighter_for_key(key).name = OBJECT_NAMES.get(kind, kind)
        self.fighter_for_key(key).status_stacks['mechanism_object'] = 1
        return key

    def _resolve_player(self, actor, choice):
        if choice.automatic:
            name = self._skill(actor, choice.skill_slot)[1].name if choice.action == ACTION_SKILL else '普通攻擊'
            self.log.append(f'{actor.name} 自動戰鬥：使用{name}。')
            choice = replace(choice, automatic=False)
        if choice.action == ACTION_DEFEND and not self.forced(actor, self.round):
            actor.passive_state.pop('witch_camera_target', None)
            self.mechanics.setdefault('embroidery_last_action', {})[str(actor.team)] = dict(
                user_id=actor.user_id, damage=False, healing=False)
            self.log.append(f'{actor.name} 防禦，本回合承受傷害減半。')
        return super()._resolve_player(actor, choice)

    def prepare(self, actor, kind=None):
        super().prepare(actor, kind)
        interval = self.rng.randint(*WITCH_RHYTHMS[actor.job][1])
        if self.phase(actor) == 2 and actor.job in ('ema', 'margo', 'arisa', 'meruru'):
            interval += 1
        self.next_cast[actor.job] = self.round + interval
        if actor.job == 'margo' and self.pending[actor.job]['phase'] == 1:
            self.pending[actor.job]['targets'] = self.pending[actor.job]['targets'][:1]
        data = self.pending[actor.job]
        if actor.job == 'coco' and data['phase'] == 0:
            taunter = next((p for p in self.living(0) if p.has('taunt', self.round)), None)
            if taunter:
                data['targets'] = [self.key(taunter)]
        if actor.job == 'ema' and kind != 'ema_followup':
            players = [p for p in self.living(0) if self.factor_stacks(p)]
            data['targets'] = [self.key(p) for p in (players if data['phase'] == 2 else
                [max(players, key=self.factor_stacks)] if players else [])]
        if actor.job in ('milia', 'margo'):
            target = self.victim(actor)
            data['followup_target'] = self.key(target) if target else None
        if kind == 'hiro_reaction':
            self.log.append(f'{actor.name}：「啊啊啊啊啊啊啊啊啊啊」')
        self.log.append(f'【{actor.name}】準備特殊魔法，下回合生效。')

    def act(self, actor):
        # Public battle conditions may advance an almost-ready spell by one turn.
        # Already announced spells, recovery and partner reactions keep priority.
        if (actor.job in self.ids and actor.job not in self.pending
                and actor.job not in self.link_spent and actor.job not in self.reactions
                and self.round > self.recovery.get(actor.job, -1)
                and self.round == self.next_cast[actor.job] - 1):
            players = self.living(0)
            opportunity = (
                actor.job == 'ema' and any(self.factor_stacks(p) >= 2 for p in players)
                or actor.job == 'meruru' and any(w.hp < w.stats['HP'] * .5 for w in self.witches())
                or actor.job == 'coco' and sum(p.has('watch', self.round) for p in players) >= 2
                or actor.job == 'arisa' and self.phase(actor) == 2
                and sum(p.has('burn', self.round) for p in players) >= 2
            )
            if opportunity:
                self.next_cast[actor.job] = self.round
                self.log.append(f'{actor.name} 因戰況提前準備魔法；仍於下一回合生效。')
        return super().act(actor)

    def intent(self):
        lines = []
        turn = self.planning_round
        for witch in self.witches():
            data = self.pending.get(witch.job)
            line = f'**{witch.name}**'
            if turn <= self.recovery.get(witch.job, -1):
                lines.append(line + '\n行動：恢復中，使用弱化普攻\n目標：行動時依挑釁選擇；無挑釁時隨機。')
                continue
            if self.active_link and witch.job in self.active_link['pair']:
                lines.append(line + f'\n行動：參與連動\n生效：第 {self.active_link["due"]} 回合；詳見下方連動預告。')
                continue
            if data:
                targets = [self.fighter_for_key(k) for k in data.get('targets', [])]
                target_names = '、'.join(p.name + ('（已倒下）' if p.hp <= 0 else '') for p in targets if p)
                if witch.job == 'hanna' or witch.job in ('arisa', 'coco') and data['phase'] == 2:
                    target_names = '全體玩家'
                if witch.job == 'meruru' or witch.job == 'reia' and data['phase'] < 2 or witch.job == 'milia' and data['phase'] == 2:
                    target_names = ''
                ability = SPELL_DESCRIPTIONS[witch.job][data['phase']]
                if data['kind'] in ('hiro_reaction', 'ema_followup'):
                    ability = '啊啊啊啊啊啊啊啊啊啊・單體反擊' if data['kind'] == 'hiro_reaction' else '回溯救援後的單體追擊'
                line += f'\n行動：施放魔法\n魔法：{ability}\n生效：第 {data["due"]} 回合'
                if target_names:
                    line += f'\n目標：{target_names}'
                    if witch.job == 'ema' and data['kind'] != 'ema_followup':
                        line += '\n魔女因子：' + '、'.join(f'{p.name} {self.factor_stacks(p, turn)} 層' for p in targets if p)
                        line += '；淨化至 0 層可阻止引爆。'
                elif witch.job != 'meruru':
                    line += '\n目標：' + ('自己' if witch.job in ('reia', 'milia') else '無有效對象')
                followup = self.fighter_for_key(data.get('followup_target'))
                if followup:
                    line += f'\n追加普攻 {followup.name}'
                if data.get('objects'):
                    line += '\n應對：可拆除物件阻止相應攻擊。'
                if data.get('rescue_target'):
                    target = self.fighter_for_key(data['rescue_target'])
                    line += f'\n救援目標：{target.name}' + ('（已倒下，救援取消）' if target.hp <= 0 else '')
                if witch.job == 'arisa' and data['phase'] == 1:
                    line += '；來源火傷未淨化時，隨機向一名未燃燒玩家擴散'
            else:
                if witch.job in self.reactions:
                    action = '同伴倒下反應'
                    detail = ('對全體玩家追加魔女因子。' if witch.job == 'ema' else
                              f'準備反應魔法，預計第 {turn + 1} 回合生效；目標於準備後公布。')
                elif (witch.job == 'ema' and self.ema_followup) or turn >= self.next_cast[witch.job]:
                    action = '準備魔法（預計）'
                    detail = f'本回合蓄力，預計第 {turn + 1} 回合生效；目標於準備後公布。'
                else:
                    action = '普通攻擊（預計）'
                    detail = '目標：行動時依挑釁選擇；無挑釁時隨機。'
                    trigger = {
                        'ema': '任一玩家魔女因子達 2 層', 'meruru': '任一魔女低於半血',
                        'coco': '至少兩名玩家被監視',
                        'arisa': '二次魔女化且至少兩名玩家有火傷',
                    }.get(witch.job)
                    if trigger and turn == self.next_cast[witch.job] - 1:
                        detail += f'\n戰況觸發：{trigger}時，改為提前蓄力；仍於第 {turn + 1} 回合生效。'
                line += f'\n行動：{action}\n{detail}'
            lines.append(line)
        if self.active_link:
            name = {('hiro', 'ema'): '希羅與艾瑪・回溯救援', ('sherry', 'hanna'): '我們有飛得這麼高過嗎？',
                    ('noah', 'anan'): '動物召喚'}[tuple(self.active_link['pair'])]
            lines.append(f'**連動｜{name}**\n生效：第 {self.active_link["due"]} 回合\n應對：可打斷。')
            target = self.fighter_for_key(self.active_link.get('target'))
            if target:
                lines.append(f'全體攻擊後追擊 {target.name}。')
        for obj in self.living(1):
            if obj.job not in ('rabbit', 'snake', 'bird'):
                continue
            spec = self.objects.get(self.key(obj), {})
            if spec.get('charging'):
                target = self.fighter_for_key(spec.get('target'))
                detail = '全體噴火' if obj.job == 'rabbit' else f'利爪攻擊 {target.name if target else "存活玩家"}'
                lines.append(f'**{obj.name}**\n行動：{detail}\n生效：第 {spec["next"]} 回合。')
        lines.append('預告依目前戰況顯示；打斷、倒下與連動可能改變行動。已公布的指定目標不會重新抽選。')
        return EnemyIntent(turn, f'第 {turn} 回合行動預告', '\n\n'.join(lines))


def witch_battle_from_participants(participants, ids, seed=None):
    if not participants:
        raise TotalRaidError('隊伍中沒有可參戰的玩家。')
    average_level = sum(p['state']['level'] for p in participants) / len(participants)
    scales = witch_stat_scales(average_level, len(participants))
    snapshot = raid_battle(participants, {'kind': '總力戰參戰資料', 'name': '總力戰參戰資料', 'strength': 1.0}, seed)
    players = [p for p in snapshot.fighters if p.team == 0]
    enemies = []
    for key in ids:
        _, name, hp, atk, defense, speed, accuracy, evasion, critical, _ = PROFILE[key]
        enemies.append(Fighter(name, 1, key, {'HP': max(1, round(hp * scales['HP'])),
            '攻擊': max(1, round(atk * scales['攻擊'])), '防禦': max(1, round(defense * scales['防禦'])),
            '治療量': 0, '命中率': accuracy, '閃避率': evasion,
            '暴擊率': critical}, speed, [], is_boss=True))
    battle = WitchRaidBattle(players + enemies, ids, seed)
    battle.mechanics['witch_average_level'] = average_level
    battle.mechanics['witch_party_size'] = len(participants)
    battle.mechanics['witch_scaling_version'] = SCALING_VERSION
    battle.mechanics['witch_stat_scales'] = scales
    battle.log.extend(snapshot.log)
    return battle


def _encode(value):
    if isinstance(value, (set, tuple)):
        return {'type': type(value).__name__, 'items': [_encode(v) for v in value]}
    if isinstance(value, dict):
        return {'type': 'dict', 'items': [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        items = value['items']
        if value['type'] == 'dict':
            return {_decode(k): _decode(v) for k, v in items}
        return (set if value['type'] == 'set' else tuple)(_decode(v) for v in items)
    return value


def dump_witch_battle(battle):
    data = dump_battle(battle)
    data.update(mode='witch_raid', witch_version=1,
                choices=[asdict(c) for c in battle.choices.values()],
                witch_state={key: _encode(getattr(battle, key)) for key in STATE_FIELDS})
    return data


def load_witch_battle(data):
    if data.get('witch_version') != 1:
        raise TotalRaidError('不支援此魔女戰鬥存檔版本。')
    base = load_battle(data)
    state = {k: _decode(v) for k, v in data['witch_state'].items()}
    battle = WitchRaidBattle(base.fighters, state['ids'], max_rounds=base.max_rounds)
    battle.round, battle.result, battle.log, battle.mechanics = base.round, base.result, base.log, base.mechanics
    battle.rng.setstate(base.rng.getstate())
    for key in STATE_FIELDS:
        setattr(battle, key, state[key])
    battle.events = Counter(battle.events)
    battle.choices = {c['user_id']: ActionChoice(**c) for c in data.get('choices', [])}
    return battle
