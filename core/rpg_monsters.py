"""Monster tiers, traits and quality; freeze these values when announcing a raid."""
import random
from decimal import Decimal


BALANCE_VERSION = 7
# Calibrated encounter tiers are content levels. Higher-level players are
# intentionally stronger when returning to these lower-tier encounters.
REFERENCE_LEVELS = {1: 10, 2: 20, 3: 30, 4: 40, 5: 50, 6: 60}
# Base victory XP is tied to encounter tier instead of the channel that hosts
# it. Quality and channel difficulty are applied after this value is selected.
TIER_VICTORY_XP = {0: 1000, 1: 300, 2: 450, 3: 600, 4: 1000, 5: 2500, 6: 4500}
# HP, attack, defense. Tiers are internal and never part of display names.
TIERS = {0: (1, 1, 1), 1: (1, 1, 1), 2: (1.5, 1.2, 1.3), 3: (1.8, 1.15, 1.15),
         4: (2.2, 1.25, 1.2), 5: (1, 1, 1), 6: (1, 1, 1)}
# tier, HP, attack, defense, speed, accuracy, evasion, critical. Speed is an
# absolute initiative value on the same narrow scale as player speed.
PROFILES = {
    # V6 profiles target roughly 15 rounds and an 80% ordinary win rate for a
    # same-tier four-role party that answers the encounter mechanics.
    # Defense is deliberately material enough to separate high and low attack,
    # while multi-enemy encounters split their attack budget across the group.
    '月影妖狐': (2, 3.7392, 1.65, 0.875, 70, 95, 23, 15),
    '血翼蝠王': (2, 3.92, 1.687, 1.0, 65, 94, 21, 10),
    '巨獸': (1, 4.212, 1.9895, 1.0, 40, 88, 10, 10),
    # The spider gives up defense for evasion.  Its larger HP budget keeps a
    # same-tier all-offense archer party from deleting it before round 7.
    '毒蛛': (1, 3.8335, 1.8492, 0.875, 65, 95, 12, 15),
    '史萊姆群': (0, 1.2, 0.8, 0.5, 55, 90, 0, 5),
    '鐵殼魔像': (2, 4.008, 1.3755, 2.2, 35, 90, 18, 5),
    '荊棘妖樹': (2, 3.1668, 2.712, 1.625, 40, 92, 19, 5),
    '哥布林戰團': (2, 5.1624, 1.116, 1.0, 55, 92, 20, 10),
    '深淵鐘龍': (3, 4.7696, 1.57, 1.4375, 45, 92, 30, 10),
    '王城傀儡師': (3, 3.8538, 2.089, 1.125, 55, 94, 31, 10),
    '瘟疫縫合獸': (3, 3.91, 2.691, 1.25, 50, 93, 29, 8),
    # Scheduled tier-four encounters use a T40 reference party. The twins
    # split both their HP and action budget between two bodies.
    '赤雷與蒼炎': (4, 2.94, 1.048, 1.15, 55, 95, 40, 10),
    '吞城鯨': (4, 3.6, 1.537, 1.25, 35, 94, 35, 8),
    # Special summon calibrated around Lv.45 equipment. It is always ordinary
    # quality and is excluded from both scheduled encounter pools.
    '城崎諾亞': (4, 3.803625, 0.9148125, 1.4375, 60, 95, 45, 12),
    # Tier five and six use final multipliers directly (their TIERS entries are
    # neutral). They target roughly 20 rounds and a 35% ordinary win rate.
    '熔爐鎧獸': (5, 17.0, 2.9, 1.69, 42, 105, 58, 10),
    '迷霧菌后': (5, 17.0, 2.5, 1.4, 52, 105, 58, 8),
    '星蝕巨神': (6, 12.5, 2.0, 1.65, 48, 108, 63, 10),
    '逆潮聖骸': (6, 9.2, 1.62, 1.55, 52, 108, 63, 10),
}

# Player-facing tactics shared by raid announcements and the /攻略 panel.
MONSTER_GUIDES = {
    '巨獸': '血量與攻擊很高、防禦偏低、速度慢；每三回合以橫掃攻擊全隊。護衛與全隊治療能穩住橫掃後的血線。',
    '月影妖狐': '血量偏低但速度與閃避很高；每三回合施放 150% 單體月影斬，並使自身閃避 +15% 到下一回合結束。高命中攻擊較能穩定壓制。',
    '血翼蝠王': '速度與閃避偏高；每兩回合施放 150% 汲血撕咬，回復實際扣血量的 30%。撕咬受挑釁反擊、閃避與減傷影響，降低承傷也會壓低回血量。',
    '哥布林戰團': '隊長與兩名打手各有獨立血量，可用群攻同時削減；隊長每三回合放棄普攻並鼓舞存活成員，攻擊 +25% 到下一回合結束。優先擊倒或暈眩隊長可阻止鼓舞。',
    '毒蛛': '血量與防禦偏低，但速度、閃避與暴擊較高；每次攻擊命中都會附加中毒。提高命中能加速擊殺，淨化可在毒傷結算前解除中毒。',
    '史萊姆群': '三隻史萊姆各有獨立血量，每隻每回合施放一次 45% 彈跳撞擊；倒下的個體會停止行動。群體攻擊可同時削血，逐隻擊倒則能快速降低來襲次數。',
    '鐵殼魔像': '防禦極高、速度慢；第 3、6、9…回合蓄力，下一回合施放 250% 單體重拳。蓄力時可用盾擊打斷，破甲與高倍率單體攻擊適合突破鐵殼。',
    '荊棘妖樹': '血量高、速度慢；每三回合放棄普攻，回復最大 HP 5% 並隨機暈眩存活玩家的 33%（向下取整）。暈眩可淨化，爆發輸出可減少再生帶來的拖延。',
    '深淵鐘龍': '免疫暈眩；蓄力時取得鐘甲，每次直接命中可削減一層。鐘甲未破會對全隊造成最高 300% 傷害，半血後鐘甲更多、蓄力更頻繁，適合用多段攻擊集中拆甲。',
    '王城傀儡師': '本體、劍傀儡與咒傀儡各有獨立血量；劍傀儡護主，咒傀儡替本體減傷。每三回合會修復傀儡；讓兩具傀儡同時倒下可使修復失敗並令本體破甲。',
    '瘟疫縫合獸': '攻擊會疊加腐敗，三層會在玩家行動前爆裂並波及全隊；腐敗者接受技能治療時怪物也會回血。先淨化再治療可避免爆裂與共享血肉，半血後兩者威脅都會提高。',
    '赤雷與蒼炎': '兩隻各持一半總 HP；赤雷只在奇數回合單體攻擊，蒼炎只在偶數回合全體攻擊，且第 4、8、12…回合改用雷炎吐息。任一倒下後兩回合會復活，須同步壓低血量；盾擊可將復活延後一次。',
    '吞城鯨': '城塞鯨脂提供 35% 減傷，需至少 150% 倍率的高傷單體攻擊或暴擊才能逐層擊破；最後一層破裂時會短暫破甲。水位每回合永久上升，達 100 後蓄力 200% 全體吞城，可用盾擊打斷。',
    '熔爐鎧獸': '三層爐甲各提供 8% 減傷；高倍率單體攻擊或暴擊可逐層擊破。每四回合補滿爐甲並蓄力爐心震爆，可在爆發前破甲或以盾擊打斷。',
    '迷霧菌后': '菌后持有 60% 總 HP，兩隻孢子各 20%。孢子每三回合膨脹，下一回合爆炸並中毒全隊；可優先擊倒或以盾擊打斷。',
    '星蝕巨神': '星核存活時使巨神減傷 20%；巨神每四回合蓄力 200% 全體星蝕墜落。擊倒星核會取消蓄力並使本體破甲，之後仍會重建。',
    '逆潮聖骸': '攻擊附加可淨化的溺印，每層在回合結束造成 2% 最大 HP 傷害。每三回合召喚逆潮法陣，擊倒法陣可阻止全體傷害與治療並使本體破甲。',
    '城崎諾亞': '免疫暈眩但可正常中毒；70% HP 前固定使用公告抽中的單色顏料，70% 以下依紅→黃→藍調色。三色構圖完成後蓄力全體未完成稿；盾擊可打斷構圖並造成破甲。',
}
# probability, equivalent level bonus, victory rewards, equipment drop chance
QUALITIES = {
    '普通': (70, 0, 1, 0.125),
    '精英': (20, 5, 1.5, 0.2),
    '首領': (8, 10, 3, 0.3),
    '傳說': (2, 20, 5, 0.5),
}


def prepare_monster(monster, quality=None):
    def product(*values):
        result = Decimal(1)
        for value in values:
            result *= Decimal(str(value))
        return float(result)

    if quality is None:
        quality = random.choices(tuple(QUALITIES), weights=[q[0] for q in QUALITIES.values()], k=1)[0]
    elif quality not in QUALITIES:
        raise ValueError('Unknown monster quality.')
    _, level_bonus, reward, drop = QUALITIES[quality]
    tier, thp, tatk, tdef, speed, hit, dodge, crit = PROFILES[monster['kind']]
    base = TIERS[tier]
    return dict(monster, balance_version=BALANCE_VERSION, quality=quality, tier=tier, profile=dict(
        hp=product(base[0], thp), attack=product(base[1], tatk),
        defense=product(base[2], tdef), speed=speed,
        # Noah remains a fixed T45 special summon while scheduled tier-four
        # encounters use the normal T40 anchor.
        level_bonus=level_bonus + (5 if monster['kind'] == '城崎諾亞' else 0),
        hit=hit, dodge=dodge, crit=crit,
        count=(3 if monster['kind'] in ('史萊姆群', '哥布林戰團', '王城傀儡師', '迷霧菌后') else
               2 if monster['kind'] in ('赤雷與蒼炎', '星蝕巨神') else 1)),
        quality_reward=reward, quality_drop=drop)


def monster_name(monster):
    quality = monster.get('quality', '普通')
    return (quality + '・' if quality != '普通' else '') + monster['name']
