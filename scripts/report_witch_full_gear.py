"""Render the measured witch balance artifacts as a reviewable Markdown report."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from core.rpg_battle import SKILLS
from core.rpg_character import ITEMS
from core.rpg_witch_catalog import PROFILE
from scripts.simulate_witch_full_gear import COMBOS, evaluate


def compact_report(report, keep_rows=True):
    """Keep per-battle outcomes but aggregate repetitive diagnostic counters."""
    for group in list(report.get('levels', {}).values()) + list(report.get('scenarios', {}).values()):
        decisions = Counter(group.get('decision_counts', {}))
        events = Counter(group.get('event_counts', {}))
        for row in group.get('rows', []):
            decisions.update(row.pop('decisions', {}))
            events.update(row.pop('events', {}))
        group['decision_counts'], group['event_counts'] = dict(decisions), dict(events)
        if not keep_rows:
            group.pop('rows', None)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--checks', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('docs/witch-full-gear-balance.md'))
    parser.add_argument('--auto-seeds', type=int, default=5)
    args = parser.parse_args()
    report = json.loads(args.validation.read_text(encoding='utf-8'))
    checks = json.loads(args.checks.read_text(encoding='utf-8'))
    lines = ['# 魔女試煉：滿裝、會解機制隊伍的 50% 勝率估算', '',
        '目標：四人標準隊（裝甲步兵／騎士／弓兵／僧侶），按公開預告做合理應對，'
        '在每日 286 種三魔女組合等權抽樣下，整體勝率約 50%。', '',
        '這是固定規則的戰鬥模擬，不是真人勝率保證，也不表示每一組魔女都有 50% 勝率。'
        '這份報告保留初版四人試算；目前已將等級曲線與部分人數補正接入戰鬥，'
        '請以 [人數補正報告](witch-party-size-balance.md) 的最新規則為準。', '',
        '## 滿裝與操作定義', '',
        '- 使用真實 Characters.snapshot 與 equip 產生角色，包含職業成長、晉升、裝備特效、'
        '戰團徽章和 Lv.50 解鎖的被動；所有可用飾品欄填滿且不重複同款。',
        '- 滿裝指下方列出的代表性完整配置，未窮舉所有裝備搭配來證明全域最優。'
        'Lv.60 以上採 T60 迷宮菁英裝；較高角色等級仍會提高基礎屬性與飾品容量。',
        '- 不含水晶、刺繡、料理、藥水、酒館餐點及占卜。不能把這份結果稱為所有系統畢業的極限滿裝。',
        '- 優先打斷危險蓄力／連動，依速度判斷是否來得及；保留打斷技能，分工拆畫作與落石。'
        '按血量治療、處理可淨化狀態、洗腦時避免反向治療、預知時避免重複行動、危險低血量時防禦。',
        '- 不讀取未來 RNG、不預演未來回合、不按每個亂數種子挑最佳操作。'
        '打斷命中失敗、冷卻不足與操作取捨仍可能造成敗北。', '',
        '## 候選倍率與獨立驗證', '',
        '倍率相對於 rpg_witch_catalog.PROFILE 原始數值，不是相對於目前已乘平均等級的數值。'
        'HP 另外乘既有的人數係數 `0.4 + 0.15 × 人數`（四人時為 1）。', '',
        '| 平均等級 | HP 倍率 | 攻擊倍率 | 防禦倍率 | 勝率 | 勝場平均回合 | 超時率 |',
        '|---:|---:|---:|---:|---:|---:|---:|']
    auto = {}
    for level, data in sorted(report['levels'].items(), key=lambda kv: int(kv[0])):
        hp, attack, defense = data['scales']
        lines.append(f'| {level} | {hp:.3f} | {attack:.3f} | {defense:.3f} | '
                     f'{data["win_rate"]:.1%} | {data["victory_rounds"]:.1f} | {data["timeout_rate"]:.1%} |')
    total = sum(v['n'] for v in report['levels'].values())
    seed_start = min(r['seed'] for r in report['levels']['50']['rows'])
    overall = sum(v['wins'] for v in report['levels'].values()) / total
    lines.extend(['', f'獨立驗證共 {total:,} 場，整體勝率 {overall:.1%}；每個等級涵蓋全部 286 種組合。'
                  f'調參使用種子 0–2，最終驗證使用種子 {seed_start} 起；最終驗證資料未用於選擇候選倍率或操作規則。', '',
                  'HP 初始倍率 = 0.7 × 該等級參考隊平均暴擊加權攻擊 / Lv.50 相同指標；'
                  '防禦使用同一比值但不乘 0.7。攻擊以滿裝隊伍生命成長為初始值，'
                  '再用實戰模擬搜尋 50% 勝率。這是分開調整耐久與威脅，並非三項一起乘等級。', '',
                  'Lv.50 範例（四人隊）：', '',
                  '| 魔女 | HP | 攻擊 | 防禦 |', '|---|---:|---:|---:|'])
    scales = report['levels']['50']['scales']
    for key in ('ema', 'anan', 'sherry'):
        profile = PROFILE[key]
        values = [max(1, round(x * s)) for x, s in zip(profile[2:5], scales)]
        lines.append(f'| {profile[1]} | {values[0]:,} | {values[1]:,} | {values[2]:,} |')
    lines.extend(['', '## 相同配置的純自動對照', '',
                  '使用相同裝備、技能、被動與候選倍率，只換成正式服自動模式：'
                  '按技能欄使用第一個冷卻完成技能、選最低 HP 比例目標。', '',
                  '| 等級 | 解機制勝率（相同種子） | 自動勝率 | 場數／模式 |', '|---:|---:|---:|---:|'])
    for level in ('20', '50', '90'):
        data = report['levels'][level]
        cases = [(ids, seed) for ids in COMBOS for seed in range(seed_start, seed_start + args.auto_seeds)]
        result = evaluate(data['participants'], cases, data['scales'], 'auto')
        matching = [r for r in data['rows'] if r['seed'] < seed_start + args.auto_seeds]
        mechanic_rate = sum(r['win'] for r in matching) / len(matching)
        auto[level] = {k: v for k, v in result.items() if k != 'rows'}
        lines.append(f'| {level} | {mechanic_rate:.1%} | {result["win_rate"]:.1%} | {result["n"]:,} |')
        print(f'Lv.{level} paired: mechanics={mechanic_rate:.1%}, auto={result["win_rate"]:.1%}', flush=True)
    lines.extend(['', '## 人數、混等與等級邊界', '',
        '以下沿用四人校準表；表中間的等級僅以線性內插做壓力測試，不是已驗證的正式曲線。', '',
        '| 情境 | 勝率 | 勝場平均回合 |', '|---|---:|---:|'])
    names = {'three_level50': 'Lv.50 三人（騎／弓／補）', 'six_level50': 'Lv.50 六人（步／步／騎／弓／弓／補）',
             'mixed_mean50': '平均 Lv.50：步20／騎40／弓60／補80',
             'mixed_mean50_low_healer': '平均 Lv.50：步80／騎60／弓40／補20',
             'four_level19': 'Lv.19 四人（內插）', 'four_level49': 'Lv.49 四人（內插）'}
    for key, data in checks['scenarios'].items():
        rounds = f'{data["victory_rounds"]:.1f}' if data['victory_rounds'] else '—'
        lines.append(f'| {names[key]} | {data["win_rate"]:.1%} | {rounds} |')
    lines.extend(['', '因此，四人表不能直接當成所有隊伍共用的正式公式。'
        '平均等級相同仍受職業所在等級影響；人數改變行動數、治療與打斷資源。'
        '正式落地應另外校準人數，並在技能／被動／裝備解鎖前後設節點，避免跨門檻直接內插。', '',
        '## 組合差異（Lv.50）', ''])
    by_combo = defaultdict(list)
    decisions = Counter(report['levels']['50'].get('decision_counts', {}))
    events = Counter(report['levels']['50'].get('event_counts', {}))
    for row in report['levels']['50']['rows']:
        by_combo[tuple(row['ids'])].append(row['win'])
        decisions.update(row.get('decisions', {}))
        events.update(row.get('events', {}))
    rates = sorted((sum(v) / len(v), k) for k, v in by_combo.items())
    lines.append(f'286 組中，樣本全敗 {sum(rate == 0 for rate, _ in rates)} 組、'
                 f'樣本全勝 {sum(rate == 1 for rate, _ in rates)} 組。'
                 '每組樣本有限，全敗／全勝不代表真實機率為 0／100%。')
    lines.extend(['', '操作驗證：Lv.50 樣本共選擇打斷 '
        f'{decisions["interrupt"]:,} 次、拆物件 {decisions["break_object"]:,} 次、'
        f'淨化 {decisions["cleanse"]:,} 次、預告防禦 {decisions["telegraph_defend"]:,} 次；'
        f'引擎記錄一般打斷 {events["interrupts"]:,} 次。', '',
        '## 參考配置明細', ''])
    for level, data in sorted(report['levels'].items(), key=lambda kv: int(kv[0])):
        lines.extend([f'### Lv.{level}', '', '| 職業 | 武器／套裝 | 飾品 | 技能順序 |', '|---|---|---|---|'])
        for p in data['participants']:
            state = p['state']
            eq = state['equipped']
            gear = '／'.join(ITEMS[eq[k]].name for k in ('武器', '套裝'))
            acc = '、'.join(ITEMS[v].name for k, v in eq.items() if k.startswith('飾品'))
            skills = '、'.join(SKILLS[state['job']][r['skill_id'] - 1].name for r in p['rules'])
            lines.append(f'| {state["job"]} | {gear} | {acc} | {skills} |')
    lines.extend(['', '## 重現', '', '從專案根目錄執行；需要可載入 core 模組的 Python。', '',
        '```text',
        'python -m scripts.simulate_witch_full_gear --mode fit --strategy mechanics --samples 286 --tuning-seeds 3 --hp-factor 0.7 --output docs/witch-mechanics-calibration.json',
        'python -m scripts.simulate_witch_full_gear --mode validate --strategy mechanics --seeds 10 --seed-start 2000 --fit-input docs/witch-mechanics-calibration.json --output docs/witch-mechanics-validation.json',
        'python -m scripts.simulate_witch_full_gear --mode checks --strategy mechanics --seeds 5 --fit-input docs/witch-mechanics-calibration.json --output docs/witch-mechanics-checks.json',
        'python -m unittest tests.test_witch_balance_policy', '```', '',
        '精確結果以隨附 JSON 中的候選倍率、角色快照與逐場記錄為準；搜尋起始區間不同可能得到略不同的候選倍率。', ''])
    args.output.write_text('\n'.join(lines), encoding='utf-8')
    args.output.with_suffix('.auto-comparison.json').write_text(json.dumps(auto, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
