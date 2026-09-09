"""Manual, restartable daily witch raid using real character snapshots."""
from collections import Counter
from dataclasses import asdict

from core.rpg_battle import ALLY_EFFECTS, FIXED_TARGETS, Fighter, dump_battle, load_battle, raid_battle
from core.rpg_total_battle import ActionChoice, EnemyIntent, TotalRaidBattle, TotalRaidError, ACTION_ATTACK, ACTION_SKILL
from core.rpg_witch_catalog import IDS, PROFILE
from core.rpg_witch_mechanics import WitchBattleV9, SUPPORT

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
    'ema': '普攻命中累積魔女因子（最多 3 層）；可淨化。',
    'hiro': '每場僅一次倒下回溯；二次魔女化時無法發動。',
    'anan': '洗腦不可淨化；打斷或擊倒安安可解除，至少保留一位存活玩家不受洗腦。',
    'noah': '拆除畫作可阻止相應攻擊。',
    'reia': '聚光結束後遭破甲。',
    'milia': '交換／適應魔法後，追加一次預告的普通攻擊。',
    'margo': '懷疑使傷害與治療降低 30%；魔法後追加一次預告的普通攻擊。',
    'nanoka': '預知下重複普攻或同名技能，本次攻擊力降低 40%。',
    'arisa': '火傷於回合結束扣除最大 HP 的 3%。',
    'sherry': '二次魔女化的重擊後遭破甲，下一回合進入恢復。',
    'hanna': '奇數回合或浮游期間，承受裝甲步兵／騎士的非必中攻擊減傷 30%；落石可拆除。',
    'coco': '普攻命中施加監視標記。',
    'meruru': '溢出治療轉為護盾，上限為救援對象最大 HP 的 12%。',
}


class WitchRaidBattle(WitchBattleV9):
    def __init__(self, fighters, ids, seed=None, max_rounds=30):
        if len(ids) != 3 or len(set(ids)) != 3 or any(k not in IDS for k in ids):
            raise TotalRaidError('每日總力戰需要三位不同魔女。')
        super().__init__(fighters, ids, seed=seed, max_rounds=max_rounds, tuning=True)
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
            targets = self.valid_targets(uid, ACTION_ATTACK)
            target = min(targets, key=lambda k: self.fighter_for_key(k).hp) if targets else None
            choice = ActionChoice(uid, ACTION_ATTACK, target, automatic=True)
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
        return key

    def _resolve_player(self, actor, choice):
        if choice.action == ACTION_DEFEND and not self.forced(actor, self.round):
            self.mechanics.setdefault('embroidery_last_action', {})[str(actor.team)] = dict(
                user_id=actor.user_id, damage=False, healing=False)
            self.log.append(f'{actor.name} 防禦，本回合承受傷害減半。')
        return super()._resolve_player(actor, choice)

    def prepare(self, actor, kind=None):
        super().prepare(actor, kind)
        if actor.job == 'margo' and self.pending[actor.job]['phase'] == 1:
            self.pending[actor.job]['targets'] = self.pending[actor.job]['targets'][:1]
        data = self.pending[actor.job]
        if actor.job == 'coco' and data['phase'] == 0:
            taunter = next((p for p in self.living(0) if p.has('taunt', self.round)), None)
            if taunter:
                data['targets'] = [self.key(taunter)]
        if actor.job == 'ema' and kind != 'ema_followup':
            players = self.living(0)
            data['targets'] = [self.key(p) for p in (players if data['phase'] == 2 else
                [max(players, key=lambda p: p.status_stacks.get('factor', 0))] if players else [])]
        if actor.job in ('milia', 'margo'):
            target = self.victim(actor)
            data['followup_target'] = self.key(target) if target else None
        if kind == 'hiro_reaction':
            self.log.append(f'{actor.name}：「啊啊啊啊啊啊啊啊啊啊」')
        self.log.append(f'【{actor.name}】準備特殊魔法，下回合生效。')

    def intent(self):
        lines = []
        for witch in self.witches():
            phase = self.phase(witch)
            data = self.pending.get(witch.job)
            line = f'{witch.name}（魔女化 {phase}）'
            if data:
                targets = [self.fighter_for_key(k) for k in data.get('targets', [])]
                target_names = '、'.join(p.name + ('（已倒下）' if p.hp <= 0 else '') for p in targets if p)
                if witch.job == 'hanna' or witch.job in ('arisa', 'coco') and data['phase'] == 2:
                    target_names = '全體玩家'
                if witch.job == 'meruru' or witch.job == 'reia' and data['phase'] < 2 or witch.job == 'milia' and data['phase'] == 2:
                    target_names = ''
                line += f'：第 {data["due"]} 回合／{SPELL_DESCRIPTIONS[witch.job][data["phase"]]}'
                if data['kind'] in ('hiro_reaction', 'ema_followup'):
                    line = f'{witch.name}：第 {data["due"]} 回合／' + ('啊啊啊啊啊啊啊啊啊啊・單體反擊' if data['kind'] == 'hiro_reaction' else '回溯救援後的單體追擊')
                if witch.job == 'anan':
                    line += '／強制普攻隊友' if any(self.forced(p, data['due']) for p in targets if p) else '／攻擊與治療反轉'
                if target_names:
                    line += f'；目標 {target_names}'
                followup = self.fighter_for_key(data.get('followup_target'))
                if followup:
                    line += f'；追加普攻 {followup.name}'
                if data.get('objects'):
                    line += '；可拆除物件阻止'
                if data.get('rescue_target'):
                    line += f'；救援 {self.fighter_for_key(data["rescue_target"]).name}'
                if witch.job == 'arisa' and data['phase'] == 1:
                    line += '；來源火傷未淨化時，隨機向一名未燃燒玩家擴散'
            else:
                line += '：普攻（依挑釁／隨機選擇）或準備魔法'
            lines.append(line)
        if self.active_link:
            name = {('hiro', 'ema'): '希羅與艾瑪・回溯救援', ('sherry', 'hanna'): '我們有飛得這麼高過嗎？',
                    ('noah', 'anan'): '動物召喚'}[tuple(self.active_link['pair'])]
            lines.append(f'連動【{name}】：第 {self.active_link["due"]} 回合；可打斷。')
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
                lines.append(f'{obj.name}：第 {spec["next"]} 回合／{detail}。')
        return EnemyIntent(self.planning_round, '魔女預告', '\n'.join(lines))


def witch_battle_from_participants(participants, ids, seed=None):
    snapshot = raid_battle(participants, {'kind': '總力戰參戰資料', 'name': '總力戰參戰資料', 'strength': 1.0}, seed)
    players = [p for p in snapshot.fighters if p.team == 0]
    enemies = []
    for key in ids:
        _, name, hp, atk, defense, speed, accuracy, evasion, critical, _ = PROFILE[key]
        enemies.append(Fighter(name, 1, key, {'HP': round(hp * (.4 + .15 * len(players))),
            '攻擊': atk, '防禦': defense, '治療量': 0, '命中率': accuracy, '閃避率': evasion,
            '暴擊率': critical}, speed, [], is_boss=True))
    battle = WitchRaidBattle(players + enemies, ids, seed)
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
