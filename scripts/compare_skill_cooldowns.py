"""Reproducible old/new cooldown comparison using the production combat engine.
Run: python -m scripts.compare_skill_cooldowns --seeds 100
Only benchmark subclasses emulate old CD; production skill values are untouched.
Uses the CURRENT skill catalog and cooldown floor. Archived reports generated
before the catalog-wide +1 adjustment do not describe the current balance.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import statistics

from core.rpg_battle import Battle, Fighter, Rule, SKILLS, default_rules, participant_fighters, raid_battle
from core.rpg_monsters import prepare_monster
from scripts.simulate_painted_maze import build_participants, JOBS
from scripts.simulate_tier56 import tactics


def rules_for(job, preset):
    if preset == 'default':
        return default_rules(job)
    ids = {
        'damage': {'裝甲步兵': (2, 5, 1), '騎士': (3, 1, 4), '弓兵': (5, 4, 1), '僧侶': (5, 2, 1)},
        'area': {'裝甲步兵': (4, 2, 1), '騎士': (3, 1, 4), '弓兵': (3, 4, 1), '僧侶': (5, 2, 4)},
        'support': {'裝甲步兵': (2, 3, 1), '騎士': (2, 1, 3), '弓兵': (2, 5, 1), '僧侶': (3, 1, 2)},
    }[preset][job]
    rules = []
    for slot, skill_id in enumerate(ids, 1):
        skill = SKILLS[job][skill_id - 1]
        condition = 'ally_debuff' if skill.effect == 'cleanse' else 'ally50' if skill.effect == 'heal' else 'always'
        target = 'debuffed' if skill.effect == 'cleanse' else 'strongest' if skill.effect == 'bless' else 'lowest'
        rules.append(Rule(slot, slot, True, condition, target, skill_id, 70 if condition == 'ally50' else None))
    return rules


class MeasuredBattle(Battle):
    def setup_metrics(self, old, training=False):
        self.old_cd = old
        self.training = training
        self.exposure = Counter()
        self.opportunities = Counter()
        self.coverage = Counter()
        self.cleansed = 0
        self.enemy_stun_skips = 0

    def _resolve_skill(self, actor, rule, skill, target):
        result = super()._resolve_skill(actor, rule, skill, target)
        # Exactly restore the previous '+ cooldown + 1', before finish-passive hooks.
        if self.old_cd:
            actor.ready[rule.slot] += 1
        return result

    def clear_negative_effects(self, target):
        removed = super().clear_negative_effects(target)
        if target.team == 0:
            self.cleansed += removed
        return removed

    def hit(self, actor, target, *args, **kwargs):
        if actor.team == 0:
            for effect, affected in (('bless', actor), ('break', target)):
                self.opportunities[effect] += 1
                self.exposure[effect] += affected.has(effect, self.round)
        else:
            for effect, affected in (('guard', target), ('weak', actor)):
                self.opportunities[effect] += 1
                self.exposure[effect] += affected.has(effect, self.round)
        return super().hit(actor, target, *args, **kwargs)

    def step(self):
        if self.training:
            # Controlled support-demand fixture, not a survival/win-rate estimate.
            # Fixed deficits avoid overhealing and deaths determining cast opportunities.
            for actor in self.living(0):
                actor.hp = max(1, actor.stats['HP'] * 45 // 100)
            if self.round % 3 == 0:
                actor = self.fighters[(self.round // 3) % 4]
                self.apply_debuff(actor, 'weak', self.round + 3)
        super().step()
        self.enemy_stun_skips += sum(
            any(line == f'{f.name} 因暈眩跳過本次行動。' for f in self.fighters if f.team == 1)
            for line in self.log)
        allies, enemies = self.living(0), self.living(1)
        for effect in ('guard', 'bless', 'taunt', 'stance', 'immunity'):
            self.coverage[effect + '_any'] += any(f.has(effect, self.round) for f in allies)
            self.coverage[effect + '_all'] += bool(allies) and all(f.has(effect, self.round) for f in allies)
        for effect in ('break', 'weak'):
            self.coverage[effect + '_any'] += any(f.has(effect, self.round) for f in enemies)
        self.log.clear()


def one_run(participants, old, cdr, seed, targets=None, kind=None):
    if targets is not None:
        fighters, _, _ = participant_fighters(deepcopy(participants))
        for i in range(targets):
            fighters.append(Fighter(f'木樁{i}', 1, '木樁',
                {'HP': 10**9, '攻擊': 100, '防禦': 180, '治療量': 0,
                 '命中率': 100, '閃避率': 15, '暴擊率': 0}, 50, [], user_id=-i-1))
        battle = MeasuredBattle(fighters, seed, max_rounds=30)
    else:
        battle = raid_battle(deepcopy(participants), prepare_monster({'kind': kind, 'name': kind}, '普通'), seed)
        battle.__class__ = MeasuredBattle
    battle.setup_metrics(old, training=targets is not None)
    for f in battle.living(0):
        f.cooldown_reduction = cdr
    while battle.result is None:
        battle.step()
    result = {'rounds': battle.round, 'win': battle.result == '勝利', 'jobs': {},
              'coverage': dict(battle.coverage), 'exposure': dict(battle.exposure),
              'opportunities': dict(battle.opportunities), 'cleansed': battle.cleansed,
              'enemy_stun_skips': battle.enemy_stun_skips}
    for f in battle.fighters[:4]:
        stats = f.combat_stats
        result['jobs'][f.job] = {**stats, 'contribution': stats['direct_damage'] + stats['support_damage']}
    if targets is not None:
        assert battle.round == 30 and all(f.hp > 0 for f in battle.fighters[:4]), 'Training fixture caused a death'
    return result


def aggregate(runs):
    rounds = sum(r['rounds'] for r in runs)
    result = {'samples': len(runs), 'rounds': rounds / len(runs),
              'win_rate': statistics.mean(r['win'] for r in runs), 'jobs': {},
              'cleansed_per30': sum(r['cleansed'] for r in runs) / rounds * 30}
    result['enemy_stun_skips_per30'] = sum(r['enemy_stun_skips'] for r in runs) / rounds * 30
    for job in JOBS:
        rows = [r['jobs'][job] for r in runs]
        values = {}
        for key in ('damage_dealt', 'direct_damage', 'support_damage', 'contribution', 'healing_done', 'overhealing', 'damage_taken'):
            values[key + '_per_round'] = sum(row[key] for row in rows) / rounds
        values['contribution_run_sd'] = statistics.stdev(r['jobs'][job]['contribution'] / r['rounds'] for r in runs) if len(runs) > 1 else 0
        skills = Counter()
        for row in rows:
            skills.update(row['skills_used'])
        values['casts_per30'] = {k: v / rounds * 30 for k, v in skills.items()}
        result['jobs'][job] = values
    for field in ('coverage', 'exposure'):
        numerator = Counter()
        denominator = Counter()
        for r in runs:
            numerator.update(r[field])
            denominator.update(r['opportunities'])
        result[field] = {k: v / (rounds if field == 'coverage' else denominator[k]) for k, v in numerator.items()}
    return result


def report(data, path):
    lines = ['# 技能 CD 新舊制模擬比較', '',
        f"每格 {data['seeds']} 個固定種子；四人隊各一名裝甲步兵、騎士、弓兵、僧侶。",
        'Lv.50／60 使用正式 Characters 快照與同階討伐武器、套裝；被動固定第 1 個，無飾品、料理、塔羅或迷宮契約。減 CD 1 為單獨注入的敏感度測試。',
        '舊制僅在模擬子類恢復 ready = round + effective_cd + 1；新制使用目前正式引擎。兩制使用相同種子、裝備與順位，但行動改變後亂數消耗也會改變，並非逐次攻擊配對。',
        '木樁每場 30 回合，1／3 隻，防禦 180、閃避 15、攻擊 100。每回合開始將隊員 HP 設為 45%，每三回合輪流施加虛弱，以固定治療／淨化需求；這是技能循環測試，不能視為生存能力或實際治療量預測。',
        '實戰使用既有 T5／T6 模擬的機制應對配技；血量自然變動，沒有木樁補血／造傷或注入負面狀態。每格每職輸出按所有樣本總傷害除以總戰鬥回合；包含倒下後回合。',
        '傷害貢獻 = 引擎 direct_damage + support_damage（含破甲、祝福增傷及毒箭等間接傷害）；不能再把 damage_dealt 加上 support_damage，否則重複計算。表格比較的是團隊中的貢獻，不是單人職業排名。',
        '覆蓋率為回合結束時效果仍有效的回合占比（any：至少一名；all：所有存活隊員）。出手覆蓋率另計攻擊嘗試當下的祝福／破甲／護衛／虛弱，包含未命中與反擊；暈眩是消耗一次行動，未以回合末殘留推估控場率。',
        '', '## 角色快照與配技', '']
    for level, party in data['profiles'].items():
        for p in party:
            lines.append(f"- Lv.{level} {p['state']['job']}：{p['state']['combat']}；裝備 {p['state']['equipped']}")
    for preset in ('default', 'damage', 'area', 'support'):
        lines.append(f'\n### {preset}（順序即優先度）\n')
        for job in JOBS:
            texts = [f"{SKILLS[job][(r.skill_id or r.slot)-1].name} [{r.condition}, {r.condition_value}, {r.target}]" for r in rules_for(job, preset)]
            lines.append(f"- {job}：" + ' → '.join(texts))
    for key, pair in data['results'].items():
        old, new = pair['old'], pair['new']
        lines += ['', f'## {key}', '', f"平均回合 {old['rounds']:.2f} → {new['rounds']:.2f}；" + (f"勝率 {old['win_rate']:.1%} → {new['win_rate']:.1%}" if key.startswith('raid') else '固定需求木樁，不解讀勝率'), '',
                  '| 職業 | 傷害貢獻／回合 舊→新 | 變化 | 有效治療／回合 舊→新 | 輔助／間接傷害 新 |', '|---|---:|---:|---:|---:|']
        for job in JOBS:
            a, b = old['jobs'][job], new['jobs'][job]
            x, y = a['contribution_per_round'], b['contribution_per_round']
            lines.append(f"| {job} | {x:.1f} → {y:.1f} | {(y/x-1)*100:+.1f}% | {a['healing_done_per_round']:.1f} → {b['healing_done_per_round']:.1f} | {b['support_damage_per_round']:.1f} |" if x else f'| {job} | 0 → {y:.1f} | — | — | — |')
        lines += ['', '| 職業 | 每 30 戰鬥回合施放次數：舊 → 新 |', '|---|---|']
        for job in JOBS:
            a, b = old['jobs'][job]['casts_per30'], new['jobs'][job]['casts_per30']
            lines.append(f'| {job} | ' + '；'.join(f'{k} {a.get(k,0):.2f} → {b.get(k,0):.2f}' for k in sorted(a.keys() | b.keys())) + ' |')
        lines += ['', '| 回合末覆蓋率 | 舊 | 新 |', '|---|---:|---:|']
        for effect in ('guard_all', 'immunity_all', 'taunt_any', 'bless_any', 'break_any', 'weak_any', 'stance_any'):
            lines.append(f"| {effect} | {old['coverage'].get(effect,0):.1%} | {new['coverage'].get(effect,0):.1%} |")
        lines += ['', '| 攻擊嘗試當下覆蓋率 | 舊 | 新 |', '|---|---:|---:|']
        for effect in ('bless', 'break', 'guard', 'weak'):
            lines.append(f"| {effect} | {old['exposure'].get(effect,0):.1%} | {new['exposure'].get(effect,0):.1%} |")
        lines.append(f"\n每 30 回合移除負面狀態數（含護衛清除）：{old['cleansed_per30']:.2f} → {new['cleansed_per30']:.2f}")
        lines.append(f"\n每 30 回合敵人因暈眩跳過行動次數：{old['enemy_stun_skips_per30']:.2f} → {new['enemy_stun_skips_per30']:.2f}")
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=100)
    parser.add_argument('--out', default='docs/balance/cooldown-comparison')
    args = parser.parse_args()
    assert args.seeds > 0
    data = {'seeds': args.seeds, 'profiles': {}, 'results': {},
            'skill_cooldowns': {job: {skill.name: skill.cooldown for skill in skills}
                                for job, skills in SKILLS.items()}}
    for level in (50, 60):
        party = build_participants(level, 4)
        data['profiles'][str(level)] = deepcopy(party)
        for preset in ('default', 'damage', 'area', 'support'):
            for p in party:
                p['rules'] = [asdict(r) for r in rules_for(p['state']['job'], preset)]
            for targets in (1, 3):
                for cdr in (0, 1):
                    key = f'training Lv{level} {preset} targets{targets} cdr{cdr}'
                    data['results'][key] = {mode: aggregate([one_run(party, mode == 'old', cdr, seed, targets=targets) for seed in range(args.seeds)]) for mode in ('old', 'new')}
                    print(key, flush=True)
        for kind in (('熔爐鎧獸', '迷霧菌后') if level == 50 else ('星蝕巨神', '逆潮聖骸')):
            for p in party:
                p['rules'] = [asdict(r) for r in tactics(kind, p['state']['job'])]
            for cdr in (0, 1):
                key = f'raid Lv{level} {kind} cdr{cdr}'
                data['results'][key] = {mode: aggregate([one_run(party, mode == 'old', cdr, seed, kind=kind) for seed in range(args.seeds)]) for mode in ('old', 'new')}
                print(key, flush=True)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix('.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    report(data, path.with_suffix('.md'))


if __name__ == '__main__':
    main()
