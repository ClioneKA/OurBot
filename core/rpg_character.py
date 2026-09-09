"""Character rules and transactional equipment storage; no Discord dependency."""
from dataclasses import dataclass, replace
import json
import math
import time

from core.rpg import level_for
from core.rpg_fishing_bosses import FISHING_BOSSES, boss_ingredient


STAT_NAMES = ('生命力', '力氣', '耐力', '靈巧', '信仰')
COMBAT_NAMES = ('HP', '攻擊', '防禦', '治療量')
BASE_SPEED = {'民兵': 50, '裝甲步兵': 45, '騎士': 40, '弓兵': 60, '僧侶': 50}
STABILITY = {'裝甲步兵': (60, 140), '騎士': (80, 120), '弓兵': (75, 125), '僧侶': (90, 110)}
# Accuracy is an equipment rating.  A weapon's rating follows the content
# level it is designed for; monsters use the same scale for evasion.
WEAPON_ACCURACY_BY_STAGE = (10, 20, 50, 90)
# An Lv.120 archer has 376 dexterity under the current growth rules.  This
# scale anchors 10 dexterity at 10% critical chance and 376 at 95%, with
# diminishing returns between and beyond those points.
CRITICAL_CURVE_SCALE = 366 / math.log(18)
# Every profession gains the same total points per level, with different priorities.
GROWTH = {
    '民兵': (2, 2, 2, 2, 2),
    '裝甲步兵': (3, 3, 2, 1, 1),
    '騎士': (4, 1, 3, 1, 1),
    '弓兵': (2, 3, 1, 3, 1),
    '僧侶': (2, 1, 1, 2, 4),
}
JOBS = tuple(job for job in GROWTH if job != '民兵')
CRITICAL_DAMAGE_PERCENT = {
    '民兵': 150,
    '裝甲步兵': 150,
    '騎士': 125,
    '弓兵': 175,
    '僧侶': 125,
}
PREFIXES = ('早期', '', '老練', '精銳')
WEAPONS = {'裝甲步兵': '戰斧', '騎士': '劍盾', '弓兵': '長弓', '僧侶': '權杖'}
SUITS = {'裝甲步兵': '步兵甲', '騎士': '騎士鎧', '弓兵': '獵裝', '僧侶': '僧袍'}
# Fixed at the canonical T10/T20/T50/T90 requirements.  A weapon and suit
# together contribute about 20% of the naked character's relevant combat
# values at that tier; the split preserves each profession's equipment role.
SHOP_EQUIPMENT = {
    '裝甲步兵': (
        ((13, 15, 3, 0), (53, 2, 14, 0)),
        ((28, 33, 6, 0), (110, 5, 25, 0)),
        ((66, 82, 14, 0), (264, 14, 56, 0)),
        ((116, 147, 24, 0), (466, 25, 96, 0)),
    ),
    '騎士': (
        ((19, 13, 5, 0), (47, 0, 12, 0)),
        ((48, 27, 11, 0), (114, 0, 27, 0)),
        ((123, 65, 28, 0), (295, 0, 68, 0)),
        ((222, 116, 51, 0), (532, 0, 121, 0)),
    ),
    '弓兵': (
        ((0, 11, 0, 0), (66, 3, 17, 0)),
        ((0, 24, 0, 0), (114, 8, 24, 0)),
        ((0, 62, 0, 0), (242, 18, 43, 0)),
        ((0, 110, 0, 0), (410, 33, 68, 0)),
    ),
    '僧侶': (
        ((0, 13, 0, 9), (66, 0, 17, 8)),
        ((0, 27, 0, 26), (114, 0, 24, 20)),
        ((0, 65, 0, 68), (242, 0, 43, 54)),
        ((0, 116, 0, 124), (410, 0, 68, 99)),
    ),
}


class CharacterError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    name: str
    slot: str
    job: str
    stage: int
    stats: tuple
    combat: tuple = (0, 0, 0, 0)
    stability: tuple = (100, 100)
    price: int = 0
    value: int = 0
    required_level: int | None = None
    party_bonus: bool = False
    speed: int = 0
    accuracy: int = 0
    evasion: int = 0
    lifesteal: int = 0
    damage_guard_chance: int = 0
    vulnerable_chance: int = 0
    vulnerable_percent: int = 0
    healing_share: int = 0
    alternating_damage_percent: int = 0
    defense_conversion: bool = False
    category: str = '裝備'
    description: str = ''
    sell_price: int | None = None
    transferable: bool = True
    socket_base: str = ''
    paint_color: str = ''
    embroidery_slots: int = 0
    crystal_slots: tuple = ()
    set_id: str = ''
    first_skill_cooldown_reduction: int = 0
    critical_points: int = 0
    healing_received_percent: int = 0


@dataclass(frozen=True)
class EquipmentInstance:
    """One owned piece of combat equipment.

    ``item_id`` always points at the shared, unmodified item definition.  Socket
    and affix rolls live on the instance so two copies can evolve separately.
    """
    instance_id: int
    guild_id: int
    user_id: int
    item_id: str
    sockets: tuple = ()
    affixes: tuple = ()

    @property
    def token(self):
        return f'instance:{self.instance_id}'


@dataclass(frozen=True)
class InventoryEntry:
    reference: str
    item_id: str
    item: Item
    quantity: int
    instance_id: int | None = None


ITEMS = {}
ITEMS['starter:club'] = Item('木棒', '武器', '', 0, (0, 0, 0, 0, 0),
                              (0, 4, 6, 0), (80, 120), sell_price=0, transferable=False)
for job in JOBS:
    for stage, prefix in enumerate(PREFIXES):
        for slot_index, (slot, names) in enumerate((('武器', WEAPONS), ('套裝', SUITS))):
            key = f'{job}:{stage}:{slot}'
            ITEMS[key] = Item(prefix + names[job], slot, job, stage, (0, 0, 0, 0, 0),
                              SHOP_EQUIPMENT[job][stage][slot_index],
                              STABILITY[job] if slot == '武器' else (100, 100),
                              (0, 500, 1500, 4000)[stage],
                              speed=stage * 5 if slot == '武器' else 0,
                              accuracy=WEAPON_ACCURACY_BY_STAGE[stage] if slot == '武器' else 0,
                              sell_price=0 if stage == 0 else None,
                              transferable=stage != 0)
for index, name in enumerate(('生命護符', '力量指環', '堅韌徽章', '靈巧吊墜', '信仰念珠')):
    ITEMS[f'accessory:{index}'] = Item(name, '飾品', '', 0,
                                      tuple(3 if i == index else 0 for i in range(5)),
                                      sell_price=0, transferable=False)

# Raid-only accessories are never included in profession supplies.
for index, name in enumerate(('魔物心核', '裂牙指環', '岩鱗徽章', '風羽吊墜', '星痕念珠')):
    ITEMS[f'raid:{index}'] = Item(name, '飾品', '', 0,
                                tuple(6 if i == index else 1 for i in range(5)), embroidery_slots=1)


# Golem-exclusive equipment: regular-stage requirements, no shop price or supplies.
ITEMS['golem:hammer'] = Item('鐵核重鎚', '武器', '裝甲步兵', 1, (0, 0, 0, 0, 0),
                            (41, 50, 9, 0), (50, 150), accuracy=20)
ITEMS['golem:sword_shield'] = Item('鐵核劍盾', '武器', '騎士', 1, (0, 0, 0, 0, 0),
                                  (71, 41, 17, 0), (70, 130), accuracy=20)
ITEMS['golem:bow'] = Item('鐵弦重弓', '武器', '弓兵', 1, (0, 0, 0, 0, 0),
                         (0, 36, 0, 0), (65, 135), accuracy=20)
ITEMS['golem:staff'] = Item('鐵核祈禱杖', '武器', '僧侶', 1, (0, 0, 0, 0, 0),
                           (0, 41, 0, 38), (85, 115), accuracy=20)


for job, key, name, bonuses in (
    ('裝甲步兵', 'infantry', '荊棘戰甲', (166, 8, 38, 0)),
    ('騎士', 'knight', '古木重鎧', (172, 0, 41, 0)),
    ('弓兵', 'archer', '藤葉獵裝', (171, 12, 36, 0)),
    ('僧侶', 'monk', '靈根僧袍', (171, 0, 36, 30)),
):
    ITEMS[f'tree:{key}'] = Item(name, '套裝', job, 1, (0, 0, 0, 0, 0), bonuses)


ITEMS['goblin:badge'] = Item('戰團徽章', '飾品', '', 1, (0, 0, 0, 0, 0),
                             required_level=20, party_bonus=True, embroidery_slots=1)
for job, key, name, bonuses in (
    ('裝甲步兵', 'axe', '掠奪者戰斧', (0, 67, 0, 0)),
    ('騎士', 'sword_shield', '掠奪者劍盾', (35, 47, 8, 0)),
    ('弓兵', 'bow', '掠奪者長弓', (0, 56, 0, 0)),
    ('僧侶', 'staff', '掠奪者權杖', (0, 47, 0, 26)),
):
    ITEMS[f'goblin:{key}'] = Item(name, '武器', job, 1, (0, 0, 0, 0, 0),
                                 bonuses, (60, 140), required_level=20, speed=7, accuracy=20)


ITEMS['fox:pendant'] = Item('月影墜飾', '飾品', '', 1, (0, 0, 0, 0, 0),
                            required_level=20, evasion=5, embroidery_slots=1)
for job, key, name, bonuses in (
    ('裝甲步兵', 'axe', '血翼戰斧', (35, 41, 8, 0)),
    ('騎士', 'sword_shield', '血翼劍盾', (60, 34, 14, 0)),
    ('弓兵', 'bow', '血翼長弓', (0, 30, 0, 0)),
    ('僧侶', 'staff', '血翼權杖', (0, 34, 0, 32)),
):
    ITEMS[f'bat:{key}'] = Item(name, '武器', job, 1, (0, 0, 0, 0, 0),
                              bonuses, STABILITY[job], required_level=20, speed=7,
                              accuracy=20, lifesteal=3)


# Tier-3 raid equipment. These pieces sit between regular T20 and veteran T50 gear.
for job, key, name, bonuses in (
    ('裝甲步兵', 'infantry', '鳴鐘戰甲', (244, 12, 53, 0)),
    ('騎士', 'knight', '鎮鐘重鎧', (240, 0, 57, 0)),
    ('弓兵', 'archer', '寂響獵裝', (238, 17, 50, 0)),
    ('僧侶', 'monk', '靜鐘僧袍', (238, 0, 50, 42)),
):
    ITEMS[f'clock:{key}'] = Item(name, '套裝', job, 1, (0, 0, 0, 0, 0), bonuses,
                                required_level=30, damage_guard_chance=5)

ITEMS['puppet:twin_charm'] = Item('雙生護符', '飾品', '', 1, (2, 2, 2, 2, 2),
                                  required_level=30, healing_share=10, embroidery_slots=1)

for job, key, name, bonuses in (
    ('裝甲步兵', 'axe', '疫骨戰斧', (58, 65, 12, 0)),
    ('騎士', 'sword_shield', '縫血劍盾', (90, 59, 21, 0)),
    ('弓兵', 'bow', '腐毒長弓', (0, 47, 0, 0)),
    ('僧侶', 'staff', '瘟心權杖', (0, 53, 0, 48)),
):
    ITEMS[f'plague:{key}'] = Item(name, '武器', job, 1, (0, 0, 0, 0, 0), bonuses,
                                 STABILITY[job], required_level=30,
                                 speed=8, accuracy=30,
                                 vulnerable_chance=5, vulnerable_percent=10)


# Tier-4 scheduled raid equipment. These T40 pieces sit between the T30 raid
# set and the regular T50 shop set; a complete set contributes about 25% of
# the Lv.40 naked values and each encounter serves two professions.
for job, key, weapon_name, weapon_combat, suit_name, suit_combat in (
    ('裝甲步兵', 'infantry', '雷牙戰斧', (64, 79, 13, 0), '雙極戰甲', (256, 13, 55, 0)),
    ('弓兵', 'archer', '蒼焰長弓', (0, 59, 0, 0), '炎翎獵裝', (239, 18, 51, 0)),
):
    ITEMS[f'twin_beast:{key}:weapon'] = Item(
        weapon_name, '武器', job, 2, (0, 0, 0, 0, 0), weapon_combat, STABILITY[job],
        required_level=40, speed=9, accuracy=40)
    ITEMS[f'twin_beast:{key}:suit'] = Item(
        suit_name, '套裝', job, 2, (0, 0, 0, 0, 0), suit_combat, required_level=40)

for job, key, weapon_name, weapon_combat, suit_name, suit_combat in (
    ('騎士', 'knight', '鯨骨劍盾', (118, 62, 26, 0), '吞潮重鎧', (282, 0, 65, 0)),
    ('僧侶', 'monk', '潮鳴權杖', (0, 62, 0, 65), '深海僧袍', (239, 0, 51, 52)),
):
    ITEMS[f'whale:{key}:weapon'] = Item(
        weapon_name, '武器', job, 2, (0, 0, 0, 0, 0), weapon_combat, STABILITY[job],
        required_level=40, speed=9, accuracy=40)
    ITEMS[f'whale:{key}:suit'] = Item(
        suit_name, '套裝', job, 2, (0, 0, 0, 0, 0), suit_combat, required_level=40)

ITEMS['twin_beast:charm'] = Item(
    '雙生獸飾品', '飾品', '', 2, (0, 0, 0, 0, 0), required_level=40,
    alternating_damage_percent=10, embroidery_slots=1)
ITEMS['whale:charm'] = Item(
    '吞城鯨飾品', '飾品', '', 2, (0, 0, 0, 0, 0), required_level=40,
    defense_conversion=True, embroidery_slots=1)


SET_BONUSES = {
    'molten_vein': ('熔脈', '武器最低穩定度提高 15 個百分點'),
    'furnace_heart': ('爐心', '本場最大 HP +5%'),
    'spore_shadow': ('孢影', '命中值 +5、暴擊率 +3 個百分點'),
    'mist_prayer': ('霧祈', '攻擊與治療量各 +4%'),
    'starforged': ('星鑄', '武器最低穩定度提高 25 個百分點'),
    'reverse_tide': ('逆潮', '本場最大 HP +7%'),
    'star_chaser': ('逐星', '命中值 +8、暴擊率 +5 個百分點'),
    'tide_rite': ('潮祀', '攻擊與治療量各 +6%'),
}


for key, item in {
    'forge:infantry:weapon': Item('熔脈重斧', '武器', '裝甲步兵', 2, (0, 0, 0, 0, 0),
                                  (96, 119, 20, 0), STABILITY['裝甲步兵'], required_level=50,
                                  speed=11, accuracy=60, set_id='molten_vein'),
    'forge:infantry:suit': Item('熔脈戰甲', '套裝', '裝甲步兵', 2, (0, 0, 0, 0, 0),
                                (383, 20, 81, 0), required_level=50, set_id='molten_vein'),
    'forge:knight:weapon': Item('爐心劍盾', '武器', '騎士', 2, (0, 0, 0, 0, 0),
                                (178, 95, 41, 0), STABILITY['騎士'], required_level=50,
                                speed=11, accuracy=60, set_id='furnace_heart'),
    'forge:knight:suit': Item('爐心重鎧', '套裝', '騎士', 2, (0, 0, 0, 0, 0),
                              (428, 0, 98, 0), required_level=50, set_id='furnace_heart'),
    'fungus:archer:weapon': Item('孢影長弓', '武器', '弓兵', 2, (0, 0, 0, 0, 0),
                                 (0, 90, 0, 0), STABILITY['弓兵'], required_level=50,
                                 speed=11, accuracy=60, set_id='spore_shadow'),
    'fungus:archer:suit': Item('孢影獵裝', '套裝', '弓兵', 2, (0, 0, 0, 0, 0),
                               (351, 26, 63, 0), required_level=50, set_id='spore_shadow'),
    'fungus:monk:weapon': Item('霧祈權杖', '武器', '僧侶', 2, (0, 0, 0, 0, 0),
                               (0, 95, 0, 99), STABILITY['僧侶'], required_level=50,
                               speed=11, accuracy=60, set_id='mist_prayer'),
    'fungus:monk:suit': Item('霧祈僧袍', '套裝', '僧侶', 2, (0, 0, 0, 0, 0),
                             (351, 0, 63, 78), required_level=50, set_id='mist_prayer'),
    'star:infantry:weapon': Item('星鑄戰斧', '武器', '裝甲步兵', 2, (0, 0, 0, 0, 0),
                                 (121, 151, 25, 0), STABILITY['裝甲步兵'], required_level=60,
                                 speed=12, accuracy=60, set_id='starforged'),
    'star:infantry:suit': Item('星鑄戰甲', '套裝', '裝甲步兵', 2, (0, 0, 0, 0, 0),
                               (484, 26, 101, 0), required_level=60, set_id='starforged'),
    'star:archer:weapon': Item('逐星長弓', '武器', '弓兵', 2, (0, 0, 0, 0, 0),
                               (0, 114, 0, 0), STABILITY['弓兵'], required_level=60,
                               speed=12, accuracy=60, set_id='star_chaser'),
    'star:archer:suit': Item('逐星獵裝', '套裝', '弓兵', 2, (0, 0, 0, 0, 0),
                             (437, 33, 76, 0), required_level=60, set_id='star_chaser'),
    'tide:knight:weapon': Item('逆潮劍盾', '武器', '騎士', 2, (0, 0, 0, 0, 0),
                               (227, 120, 52, 0), STABILITY['騎士'], required_level=60,
                               speed=12, accuracy=60, set_id='reverse_tide'),
    'tide:knight:suit': Item('逆潮重鎧', '套裝', '騎士', 2, (0, 0, 0, 0, 0),
                             (545, 0, 125, 0), required_level=60, set_id='reverse_tide'),
    'tide:monk:weapon': Item('潮祀權杖', '武器', '僧侶', 2, (0, 0, 0, 0, 0),
                             (0, 120, 0, 126), STABILITY['僧侶'], required_level=60,
                             speed=12, accuracy=60, set_id='tide_rite'),
    'tide:monk:suit': Item('潮祀僧袍', '套裝', '僧侶', 2, (0, 0, 0, 0, 0),
                           (437, 0, 76, 101), required_level=60, set_id='tide_rite'),
}.items():
    ITEMS[key] = item

# T60 Painted Witch elite equipment.  The combined naked weapon + suit budget
# is approximately 35% of the relevant Lv.60 combat values.  Its long-term
# identity comes from three typed crystal sockets rather than a fixed set bonus.
ELITE_CRYSTAL_SLOTS = ('outline', 'color', 'source')
for key, item in {
    'maze:infantry:weapon': Item(
        '未竟戰繪・斷彩戰斧', '武器', '裝甲步兵', 2, (0, 0, 0, 0, 0),
        (137, 171, 28, 0), STABILITY['裝甲步兵'], required_level=60,
        speed=13, accuracy=65, sell_price=8000, crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:infantry:suit': Item(
        '未竟戰繪・重彩戰甲', '套裝', '裝甲步兵', 2, (0, 0, 0, 0, 0),
        (547, 29, 114, 0), required_level=60, sell_price=8000,
        crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:knight:weapon': Item(
        '未竟守望・界框劍盾', '武器', '騎士', 2, (0, 0, 0, 0, 0),
        (257, 136, 59, 0), STABILITY['騎士'], required_level=60,
        speed=13, accuracy=65, sell_price=8000, crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:knight:suit': Item(
        '未竟守望・定框重甲', '套裝', '騎士', 2, (0, 0, 0, 0, 0),
        (616, 0, 141, 0), required_level=60, sell_price=8000,
        crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:archer:weapon': Item(
        '未竟追彩・流彩長弓', '武器', '弓兵', 2, (0, 0, 0, 0, 0),
        (0, 129, 0, 0), STABILITY['弓兵'], required_level=60,
        speed=13, accuracy=65, sell_price=8000, crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:archer:suit': Item(
        '未竟追彩・風描獵裝', '套裝', '弓兵', 2, (0, 0, 0, 0, 0),
        (494, 37, 86, 0), required_level=60, sell_price=8000,
        crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:monk:weapon': Item(
        '未竟聖像・調色聖杖', '武器', '僧侶', 2, (0, 0, 0, 0, 0),
        (0, 136, 0, 142), STABILITY['僧侶'], required_level=60,
        speed=13, accuracy=65, sell_price=8000, crystal_slots=ELITE_CRYSTAL_SLOTS),
    'maze:monk:suit': Item(
        '未竟聖像・祈彩法衣', '套裝', '僧侶', 2, (0, 0, 0, 0, 0),
        (494, 0, 86, 114), required_level=60, sell_price=8000,
        crystal_slots=ELITE_CRYSTAL_SLOTS),
}.items():
    ITEMS[key] = item

MAZE_CHOICE_BOXES = {
    'maze:choice_box:infantry': ('maze:infantry:weapon', 'maze:infantry:suit'),
    'maze:choice_box:knight': ('maze:knight:weapon', 'maze:knight:suit'),
    'maze:choice_box:archer': ('maze:archer:weapon', 'maze:archer:suit'),
    'maze:choice_box:monk': ('maze:monk:weapon', 'maze:monk:suit'),
}
for box_id, choices in MAZE_CHOICE_BOXES.items():
    job = ITEMS[choices[0]].job
    ITEMS[box_id] = Item(
        f'繪畫魔女・{job}菁英裝備自選箱', '', '', 0, (0, 0, 0, 0, 0),
        category='製作材料', transferable=False,
        description=f'可從背包使用，選擇一件 {job} T60 菁英武器或套裝。')

ITEMS['cycle:emblem'] = Item(
    '循環徽記', '飾品', '', 2, (3, 3, 3, 3, 3), required_level=60,
    embroidery_slots=1, first_skill_cooldown_reduction=1)

for key, name, description in (
    ('paint:red', '紅色噴漆罐', '擊敗深淵鐘龍時由全隊抽選一人取得，諾亞也可能掉落；可組成套組，或用於諾亞裝備染色。'),
    ('paint:yellow', '黃色噴漆罐', '擊敗王城傀儡師時由全隊抽選一人取得，諾亞也可能掉落；可組成套組，或用於諾亞裝備染色。'),
    ('paint:blue', '藍色噴漆罐', '擊敗瘟疫縫合獸時由全隊抽選一人取得，諾亞也可能掉落；可組成套組，或用於諾亞裝備染色。'),
):
    ITEMS[key] = Item(name, '', '', 0, (0, 0, 0, 0, 0), category='製作材料',
                      description=description)

ITEMS['paint:set'] = Item(
    '噴漆罐套組', '', '', 0, (0, 0, 0, 0, 0), category='製作材料',
    description='由紅、黃、藍色噴漆罐各一罐組合；可在背包中使用，召喚特殊四階討伐「城崎諾亞」。')
ITEMS['noah:unfinished'] = Item(
    '未完成的魔女畫作', '', '', 0, (0, 0, 0, 0, 0), category='製作材料',
    description='城崎諾亞留下的未完成畫作；可從背包使用，開啟繪境迷廊的城崎諾亞路線。')
ITEMS['proof:raid'] = Item(
    '討伐之證', '', '', 0, (0, 0, 0, 0, 0), category='製作材料',
    description='一至三階討伐的勝利證明；可在商店累積兌換《氣球》的畫作。',
    transferable=False)
ITEMS['painting:balloon'] = Item(
    '《氣球》的畫作', '', '', 0, (0, 0, 0, 0, 0), category='製作材料',
    description='可開啟繪境迷廊的繪畫之影路線；不會遭遇繪畫魔女，也不會掉落菁英裝備。')

BALLOON_PAINTING_PROOF_COST = 30

PAINT_ITEMS = {'red': 'paint:red', 'yellow': 'paint:yellow', 'blue': 'paint:blue'}
PAINT_NAMES = {'red': '紅色', 'yellow': '黃色', 'blue': '藍色'}
DYE_PRICE = 1_000
EMBROIDERY_PRICE = 500
EMBROIDERIES = {
    'heart': ('愛心刺繡', 'stat:0', 2),
    'flame': ('火焰刺繡', 'stat:1', 2),
    'shield': ('盾牌刺繡', 'stat:2', 2),
    'wing': ('羽翼刺繡', 'stat:3', 2),
    'star': ('星光刺繡', 'stat:4', 2),
}
NOAH_EQUIPMENT = {}
for job, slug, weapon_name, weapon_combat, suit_name, suit_combat in (
    ('裝甲步兵', 'infantry', '緋彩戰斧', (70, 86, 15, 0), '潑彩戰甲', (276, 15, 59, 0)),
    ('騎士', 'knight', '調色劍盾', (132, 67, 30, 0), '畫框重鎧', (312, 0, 72, 0)),
    ('弓兵', 'archer', '虹跡長弓', (0, 65, 0, 0), '顏料獵裝', (255, 19, 46, 0)),
    ('僧侶', 'monk', '繪夢權杖', (0, 69, 0, 72), '白紙僧袍', (255, 0, 46, 57)),
):
    for slot, name, combat in (('武器', weapon_name, weapon_combat), ('套裝', suit_name, suit_combat)):
        base_key = f'noah:{slug}:{"weapon" if slot == "武器" else "suit"}'
        base = Item(name, slot, job, 2, (0, 0, 0, 0, 0), combat,
                    STABILITY[job] if slot == '武器' else (100, 100),
                    required_level=45, socket_base=base_key,
                    accuracy=45 if slot == '武器' else 0,
                    description='可在漢娜的裁縫所使用噴漆染色。')
        ITEMS[base_key] = base
        NOAH_EQUIPMENT.setdefault(job, []).append(base_key)
        for color in ('red', 'yellow', 'blue'):
            colored_combat = combat
            speed = evasion = guard = 0
            accuracy = base.accuracy
            if color == 'red':
                values = list(combat)
                index = 1 if slot == '武器' else 0
                values[index] = values[index] * (130 if slot == '武器' else 150) // 100
                colored_combat = tuple(values)
            elif color == 'yellow':
                if slot == '武器':
                    accuracy += 5
                else:
                    speed = 15
            elif slot == '武器':
                guard = 5
            else:
                evasion = 5
            ITEMS[f'{base_key}:{color}'] = replace(
                base, name=f'{name}・{PAINT_NAMES[color]}染色', combat=colored_combat,
                speed=speed, accuracy=accuracy, evasion=evasion,
                damage_guard_chance=guard, paint_color=color,
                description=f'以{PAINT_NAMES[color]}噴漆染色，可再次改色；原顏料不返還。')


def _cooking_ingredient_description(tag, quality, pairing, aftertaste=0):
    echo = f'+{aftertaste}' if aftertaste else '—'
    return f'料理標籤：{tag}｜品質：{quality}｜餘韻：{echo}｜推薦搭配：{pairing}。'


for spot_id, fishing_boss in FISHING_BOSSES.items():
    ITEMS[boss_ingredient(spot_id)] = Item(
        fishing_boss.ingredient_name, '料理素材', '', 0, (0, 0, 0, 0, 0), category='料理素材',
        description=_cooking_ingredient_description('幸運・盛宴・餘韻', fishing_boss.quality, '—', 1),
        sell_price=400)


# Fishing items use the existing stackable inventory while remaining separate from
# combat equipment.  sell_price is the exact shop payout; legacy equipment keeps
# using 20% of its value.
for key, name, category, description, sell_price in (
    ('fishing:pond:common', '池塘鯽魚', '料理素材',
     _cooking_ingredient_description('成長', 1, '馬鈴薯'), 25),
    ('fishing:pond:rare', '七彩錦魚', '料理素材',
     _cooking_ingredient_description('幸運', 2, '小麥'), 50),
    ('fishing:pond:weed', '青苔水草', '料理素材',
     _cooking_ingredient_description('滋養', 1, '晨露藥草'), 30),
    ('fishing:pond:coin', '許願銅幣', '換金道具', '從許願池撈起的銅幣，可賣給商店換取金幣。', 100),
    ('fishing:pond:rod', '濕潤木枝', '製作材料', '適合削成釣竿竿身的木枝。', 20),
    ('fishing:pond:line', '廢棄釣線', '製作材料', '纏在池底、仍可重新利用的釣線。', 20),
    ('fishing:pond:hook', '生鏽魚鉤', '製作材料', '清理後還能使用的舊魚鉤。', 20),
    ('fishing:lake:common', '魔女湖鱒', '料理素材',
     _cooking_ingredient_description('猛攻', 2, '魔女番茄'), 50),
    ('fishing:lake:rare', '星紋魔女鰻', '料理素材',
     _cooking_ingredient_description('幸運', 3, '火紅辣椒'), 100),
    ('fishing:lake:weed', '月光水草', '料理素材',
     _cooking_ingredient_description('滋養', 2, '月鈴草'), 60),
    ('fishing:lake:coin', '沉水銀幣', '換金道具', '沉在魔女島湖底的銀幣，可賣給商店換取金幣。', 200),
    ('fishing:lake:rod', '魔力漂流木', '製作材料', '帶有微弱魔力、適合製作竿身的木材。', 40),
    ('fishing:lake:line', '魔力釣線', '製作材料', '能承受魔力魚掙扎的堅韌釣線。', 40),
    ('fishing:lake:hook', '魔力魚鉤', '製作材料', '刻有簡單魔法紋路的魚鉤。', 40),
    ('fishing:waterway:common', '幽光盲魚', '料理素材',
     _cooking_ingredient_description('成長', 3, '夜色南瓜'), 100),
    ('fishing:waterway:rare', '鏡蝶魚', '料理素材',
     _cooking_ingredient_description('幸運', 4, '月白米'), 200),
    ('fishing:waterway:weed', '夜露水草', '料理素材',
     _cooking_ingredient_description('滋養', 3, '夢霧草'), 120),
    ('fishing:waterway:coin', '褪色金幣', '換金道具', '被地下水流沖刷得看不清圖案的舊金幣。', 400),
    ('fishing:waterway:rod', '黑檀漂流木', '製作材料', '質地堅硬、沉著黑亮的高級竿身材料。', 80),
    ('fishing:waterway:line', '月蠶釣線', '製作材料', '由月蠶絲製成、幾乎透明的堅韌釣線。', 80),
    ('fishing:waterway:hook', '黯銀魚鉤', '製作材料', '在黑暗中仍泛著銀光的銳利魚鉤。', 80),
    ('fishing:bay:common', '潮紋旗魚', '料理素材',
     _cooking_ingredient_description('猛攻', 4, '星穗豆'), 200),
    ('fishing:bay:rare', '蝕星龍魚', '料理素材',
     _cooking_ingredient_description('幸運', 5, '熔心薑'), 400),
    ('fishing:bay:weed', '逆潮海帶', '料理素材',
     _cooking_ingredient_description('滋養', 4, '霧露菇'), 240),
    ('fishing:bay:coin', '星砂古幣', '換金道具', '從魔女島海灣撈起的古幣，可賣給商店換取金幣。', 800),
    ('fishing:bay:rod', '星木竿胚', '製作材料', '帶有星紋的堅韌木材，適合製作高階竿身。', 160),
    ('fishing:bay:line', '潮蠶釣線', '製作材料', '以潮蠶絲編成、能承受海灣巨物的釣線。', 160),
    ('fishing:bay:hook', '星銀魚鉤', '製作材料', '以星銀鍛成、在海霧中仍會發光的魚鉤。', 160),
):
    ITEMS[key] = Item(name, category, '', 0, (0, 0, 0, 0, 0), category=category,
                      description=description, sell_price=sell_price)

for key, name, description in (
    ('fishing:rod:old', '老舊釣竿', '初次釣魚時由安安贈送，沒有額外效果。'),
    ('fishing:rod:simple', '簡易釣竿', '收竿時有 20% 機率追加一次捕獲。'),
    ('fishing:rod:magic', '魔力釣竿', '收竿時有 30% 機率追加一次捕獲，稀有魚權重提高 10%。'),
    ('fishing:rod:glow', '幽光釣竿', '收竿時有 40% 機率追加一次捕獲，稀有魚權重提高 20%。'),
    ('fishing:rod:star_tide', '星潮釣竿', '收竿時有 50% 機率追加一次捕獲，稀有魚權重提高 20%。'),
):
    ITEMS[key] = Item(name, '釣竿', '', 0, (0, 0, 0, 0, 0), category='釣竿',
                      description=description, transferable=False)

for key, name, category, description, sell_price in (
    ('farming:potato', '馬鈴薯', '料理素材',
     _cooking_ingredient_description('盛宴', 1, '池塘鯽魚'), 25),
    ('farming:dew_herb', '晨露藥草', '料理素材',
     _cooking_ingredient_description('活力', 1, '青苔水草'), 30),
    ('farming:wheat', '小麥', '料理素材',
     _cooking_ingredient_description('盛宴', 2, '七彩錦魚'), 50),
    ('farming:witch_tomato', '魔女番茄', '料理素材',
     _cooking_ingredient_description('活力', 2, '魔女湖鱒'), 50),
    ('farming:moonbell', '月鈴草', '料理素材',
     _cooking_ingredient_description('猛攻', 2, '月光水草'), 60),
    ('farming:chili', '火紅辣椒', '料理素材',
     _cooking_ingredient_description('猛攻', 3, '星紋魔女鰻'), 100),
    ('farming:night_pumpkin', '夜色南瓜', '料理素材',
     _cooking_ingredient_description('活力', 3, '幽光盲魚'), 100),
    ('farming:dreammist_herb', '夢霧草', '料理素材',
     _cooking_ingredient_description('成長', 3, '夜露水草'), 120),
    ('farming:moonwhite_rice', '月白米', '料理素材',
     _cooking_ingredient_description('盛宴', 4, '鏡蝶魚'), 200),
    ('farming:star_bean', '星穗豆', '料理素材',
     _cooking_ingredient_description('盛宴', 4, '潮紋旗魚'), 200),
    ('farming:mist_mushroom', '霧露菇', '料理素材',
     _cooking_ingredient_description('活力', 4, '逆潮海帶'), 240),
    ('farming:ember_ginger', '熔心薑', '料理素材',
     _cooking_ingredient_description('猛攻', 5, '蝕星龍魚'), 400),
):
    ITEMS[key] = Item(name, category, '', 0, (0, 0, 0, 0, 0), category=category,
                      description=description, sell_price=sell_price)

for key, name, quality, description, sell_price in (
    ('cooking:meat:low', '鮮獸肉', 2, '討伐取得的鮮肉。', 60),
    ('cooking:meat:mid', '魔獸里肌', 3, '蘊含魔力的厚實里肌。', 120),
    ('cooking:meat:high', '星露霜肉', 4, '覆著星露薄霜的高階肉品。', 240),
):
    ITEMS[key] = Item(name, '料理素材', '', 0, (0, 0, 0, 0, 0), category='料理素材',
                      description=(f'{description}料理標籤：盛宴｜品質：{quality}｜餘韻：—｜推薦搭配：—。'),
                      sell_price=sell_price)

for key, name, score, aftertaste, description, sell_price in (
    ('cooking:seasoning:low', '粗磨岩鹽', 4, 2, '帶有礦物香氣的粗鹽。', 100),
    ('cooking:seasoning:mid', '魔女香料', 8, 3, '魔女島流傳的複合香料。', 200),
    ('cooking:seasoning:high', '星砂香料', 12, 4, '在星光下研磨完成的珍稀香料。', 400),
):
    ITEMS[key] = Item(name, '料理素材', '', 0, (0, 0, 0, 0, 0), category='料理素材',
                      description=(f'{description}調味料｜美味度 +{score}｜餘韻：+{aftertaste}｜每桌最多一份。'),
                      sell_price=sell_price)

for key, name, description, sell_price in (
    ('food:pond:common', '鯽魚馬鈴薯湯', 'HP 首次降至 40% 以下時回復最大 HP 的 15%。', 50),
    ('food:pond:rare', '香酥七彩錦魚', 'HP 首次降至 40% 以下時回復 15%，後續兩回合各回復 5%。', 100),
    ('food:lake:common', '番茄湖鱒燉湯', 'HP 首次降至 40% 以下時回復最大 HP 的 25%。', 100),
    ('food:lake:rare', '香辣星紋魔女鰻', 'HP 首次降至 40% 以下時回復 25%，後續兩回合各回復 7.5%。', 200),
    ('food:waterway:common', '幽光魚南瓜濃湯', 'HP 首次降至 40% 以下時回復最大 HP 的 35%。', 200),
    ('food:waterway:rare', '月白鏡蝶魚茶泡飯', 'HP 首次降至 40% 以下時回復 35%，後續兩回合各回復 10%。', 400),
):
    ITEMS[key] = Item(name, '料理', '', 0, (0, 0, 0, 0, 0), category='料理',
                      description=description, sell_price=sell_price)

_POTION_KINDS = {
    'hp': ('生命', '最大 HP'), 'attack': ('強攻', '攻擊'), 'defense': ('硬化', '防禦'),
    'healing': ('治癒', '治療量'), 'hit': ('專注', '命中值'),
    'evasion': ('靈敏', '閃避值'), 'critical': ('會心', '暴擊率'),
}
for tier, prefix, percent, chance, sell_price in (
    (1, '初級', 5, (3, 2, 3), 100),
    (2, '中級', 8, (5, 3, 5), 200),
    (3, '高級', 11, (7, 4, 7), 400),
):
    for kind, (name, stat) in _POTION_KINDS.items():
        amount = {'hit': chance[0], 'evasion': chance[1], 'critical': chance[2]}.get(kind, percent)
        unit = ' 個百分點' if kind == 'critical' else ('' if kind in ('hit', 'evasion') else '%')
        ITEMS[f'potion:{tier}:{kind}'] = Item(
            f'{prefix}{name}藥水', '藥水', '', 0, (0, 0, 0, 0, 0), category='藥水',
            description=f'下一場討伐使{stat} +{amount}{unit}，效果持續整場。', sell_price=sell_price)


for key, item in list(ITEMS.items()):
    if key.startswith('raid:'):
        ITEMS[key] = replace(item, value=300)
    elif key.startswith('noah:') and item.slot in ('武器', '套裝'):
        ITEMS[key] = replace(item, value=1200)
    elif key.startswith(('twin_beast:', 'whale:')):
        ITEMS[key] = replace(item, value=1000)
    elif key.startswith(('forge:', 'fungus:')):
        ITEMS[key] = replace(item, value=1250)
    elif key.startswith(('star:', 'tide:', 'cycle:')):
        ITEMS[key] = replace(item, value=1500)
    elif key.startswith(('golem:', 'tree:', 'goblin:', 'fox:', 'bat:', 'clock:', 'puppet:', 'plague:')):
        ITEMS[key] = replace(item, value=750)


def item_value(item):
    return item.price or item.value


def item_sell_price(item):
    return item.sell_price if item.sell_price is not None else item_value(item) // 5


def item_sellable(item):
    return item.sell_price is not None or item_value(item) > 0


def stage_for(level, settings):
    return sum(level >= threshold for threshold in
               (settings.regular_level, settings.veteran_level, settings.elite_level))


def stage_level(stage, settings):
    return (10, settings.regular_level, settings.veteran_level, settings.elite_level)[stage]


def stat_text(stats):
    return '、'.join(f'{name} +{value}' for name, value in zip(STAT_NAMES, stats) if value) or '無加成'


def item_level(item, settings):
    if item.required_level is not None:
        return item.required_level
    return stage_level(item.stage, settings) if item.job else 1


def combat_from_stats(total, job='民兵'):
    vitality, strength, endurance, dexterity, faith = total
    attack = {
        '裝甲步兵': strength * 3,
        '弓兵': strength + dexterity * 3 // 2,
        '騎士': strength + vitality * 5 // 4,
        '僧侶': strength + faith * 5 // 4,
    }.get(job, strength * 2)
    return {'HP': 50 + vitality * 10, '攻擊': attack,
            '防禦': endurance * 3, '治療量': faith * 3,
            '命中率': 95,
            '閃避率': min(35, max(0, dexterity) * 35 // 376),
            '暴擊率': min(100, 10 + round(90 * (1 - math.exp(-max(0, dexterity - 10)
                                                               / CRITICAL_CURVE_SCALE))))}


def speed_from_equipment(job, equipped=()):
    """Return level-independent initiative from profession and equipped items."""
    keys = equipped.values() if isinstance(equipped, dict) else equipped
    return max(1, min(100, BASE_SPEED.get(job, 50) + sum(ITEMS[key].speed for key in keys if key in ITEMS)))


def item_display_name(item):
    """Show canonical equipment tier, independent of configurable wear levels."""
    if item.slot not in ('武器', '套裝', '飾品'):
        return item.name
    tier = item.required_level if item.required_level is not None else (10, 20, 50, 90)[item.stage]
    return f'T{tier}｜{item.name}'


def equipment_slot_text(state):
    return '\n'.join(
        f'{slot}：{item_display_name(ITEMS[state["equipped"][slot]])}'
        if slot in state['equipped'] else f'{slot}：空'
        for slot in state['slots'])


def inventory_entry_label(entry, equipped_ids):
    status = '【已裝備】' if entry.instance_id in equipped_ids else ''
    identity = f' #{entry.instance_id}' if entry.instance_id else ''
    return f'{status}{item_display_name(entry.item)}{identity}'[:100]


def item_text(item):
    parts = [f'{name} +{value}' for name, value in zip(STAT_NAMES, item.stats) if value]
    parts += [f'{name} +{value}' for name, value in zip(COMBAT_NAMES, item.combat) if value]
    if item.slot == '武器':
        parts.append(f'穩定度 {item.stability[0]}–{item.stability[1]}%')
    if item.required_level is not None:
        parts.append(f'Lv.{item.required_level}')
    if item.party_bonus:
        parts.append('開戰每兩名參戰者（不足兩名進位）使五項能力各 +1，最多各 +5，整場固定，僅自身')
    if item.speed:
        parts.append(f'速度 {item.speed:+d}')
    if item.accuracy:
        parts.append(f'命中值 +{item.accuracy}')
    if item.evasion:
        parts.append(f'閃避值 +{item.evasion}')
    if item.lifesteal:
        parts.append(f'吸血 {item.lifesteal}%（直接傷害實際扣血量）')
    if item.damage_guard_chance:
        parts.append(f'受到直接傷害時有 {item.damage_guard_chance}% 機率使該次最終傷害降低 50%')
    if item.vulnerable_chance:
        parts.append(f'每次直接命中有 {item.vulnerable_chance}% 機率使目標易傷 +{item.vulnerable_percent}% 至下一回合結束')
    if item.healing_share:
        parts.append(f'實際受到治療時，額外治療血量比例最低的另一名隊友 {item.healing_share}%')
    if item.alternating_damage_percent:
        parts.append(f'奇數回合單體直接攻擊 +{item.alternating_damage_percent}%；'
                     f'偶數回合群體直接攻擊 +{item.alternating_damage_percent}%')
    if item.defense_conversion:
        parts.append('受到直接攻擊時，將最近一次由防禦擋下的傷害轉為下一次攻擊的額外攻擊力')
    if item.embroidery_slots:
        parts.append(f'刺繡格 {item.embroidery_slots} 格')
    if item.crystal_slots:
        names = {'outline': '輪廓', 'color': '色彩', 'source': '源色'}
        parts.append('鑲嵌格：' + '／'.join(names[slot] for slot in item.crystal_slots))
    if item.set_id:
        set_name, set_effect = SET_BONUSES[item.set_id]
        parts.append(f'{set_name}套裝（2 件）：{set_effect}')
    if item.first_skill_cooldown_reduction:
        parts.append('每場第一次成功施放基礎冷卻至少 2 回合的主動技能時，'
                     f'該次冷卻 -{item.first_skill_cooldown_reduction}（最低 1 回合）')
    if item.description:
        parts.append(item.description)
    return '、'.join(parts) or '無加成'


def add_owned_item(db, guild_id, user_id, item_id, quantity=1):
    """Credit stackables or create independent combat-equipment instances.

    The caller controls the transaction, which lets raid settlement keep loot,
    XP, gold, and its idempotency marker in one atomic commit.
    """
    item = ITEMS.get(item_id)
    if not item or quantity < 1:
        raise CharacterError('無效的物品。')
    if item.slot not in ('武器', '套裝', '飾品'):
        db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
            VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,item_id)
            DO UPDATE SET quantity=quantity+excluded.quantity''',
            (guild_id, user_id, item_id, quantity))
        return []
    base_id = item.socket_base if item.socket_base and item.paint_color else item_id
    socket_item_id = PAINT_ITEMS.get(item.paint_color) if item.paint_color else None
    ids = []
    for _ in range(quantity):
        cursor = db.execute('''INSERT INTO rpg_equipment_instances
            (guild_id,user_id,item_id,created_at) VALUES (?,?,?,?)''',
            (guild_id, user_id, base_id, int(time.time())))
        instance_id = cursor.lastrowid
        ids.append(instance_id)
        if socket_item_id:
            db.execute('INSERT INTO rpg_instance_sockets VALUES (?,?,?)',
                       (instance_id, 0, socket_item_id))
    return ids


class Characters:
    def __init__(self, store, settings):
        self.store = store
        self.db = store.db
        self.settings = settings
        # Separate tables leave all legacy XP and cooldown values intact.
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_characters (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, job TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_inventory (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, item_id))''')
            if 'quantity' not in {row[1] for row in self.db.execute('PRAGMA table_info(rpg_inventory)')}:
                self.db.execute('ALTER TABLE rpg_inventory ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_starter_claims (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id))''')
            # Record starter ownership before legacy equipment is removed from the
            # stackable inventory by the instance migration below.
            self.db.execute('''INSERT OR IGNORE INTO rpg_starter_claims
                SELECT guild_id,user_id FROM rpg_inventory WHERE item_id='starter:club' ''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_character_profiles (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                showcase_item_id TEXT,
                PRIMARY KEY (guild_id, user_id))''')
            if 'showcase_instance_id' not in {row[1] for row in self.db.execute(
                    'PRAGMA table_info(rpg_character_profiles)')}:
                self.db.execute('ALTER TABLE rpg_character_profiles ADD COLUMN showcase_instance_id INTEGER')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_equipment_instances (
                instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL, created_at INTEGER NOT NULL)''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS rpg_equipment_instances_owner
                ON rpg_equipment_instances(guild_id,user_id)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_instance_sockets (
                instance_id INTEGER NOT NULL, socket_index INTEGER NOT NULL,
                socket_item_id TEXT NOT NULL,
                PRIMARY KEY(instance_id,socket_index),
                FOREIGN KEY(instance_id) REFERENCES rpg_equipment_instances(instance_id) ON DELETE CASCADE)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_instance_affixes (
                instance_id INTEGER NOT NULL, affix_index INTEGER NOT NULL,
                affix_id TEXT NOT NULL, effect_key TEXT NOT NULL, rolled_value INTEGER NOT NULL,
                PRIMARY KEY(instance_id,affix_index),
                FOREIGN KEY(instance_id) REFERENCES rpg_equipment_instances(instance_id) ON DELETE CASCADE)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_schema_migrations (
                name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)''')
            self._migrate_equipment_storage()

    @staticmethod
    def _is_instance_item(item_id):
        item = ITEMS.get(item_id)
        return bool(item and item.slot in ('武器', '套裝', '飾品'))

    def _insert_instance(self, guild_id, user_id, item_id):
        """Insert one instance. Caller owns the surrounding transaction."""
        return add_owned_item(self.db, guild_id, user_id, item_id)[0]

    def _create_equipment_table(self):
        self.db.execute('''CREATE TABLE rpg_equipment (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, slot TEXT NOT NULL,
            instance_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, slot),
            UNIQUE (guild_id, user_id, instance_id),
            FOREIGN KEY(instance_id) REFERENCES rpg_equipment_instances(instance_id) ON DELETE CASCADE)''')

    def _migrate_equipment_storage(self):
        """Atomically expand legacy equipment quantities into owned instances."""
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_equipment)')}
        legacy_rows = []
        had_legacy_table = False
        if not columns:
            self._create_equipment_table()
        elif 'instance_id' not in columns:
            had_legacy_table = True
            legacy_rows = self.db.execute(
                'SELECT guild_id,user_id,slot,item_id FROM rpg_equipment').fetchall()
            self.db.execute('ALTER TABLE rpg_equipment RENAME TO rpg_equipment_legacy')
            self._create_equipment_table()

        created = {}
        rows = self.db.execute(
            'SELECT guild_id,user_id,item_id,quantity FROM rpg_inventory').fetchall()
        for guild_id, user_id, item_id, quantity in rows:
            if not self._is_instance_item(item_id):
                continue
            ids = [self._insert_instance(guild_id, user_id, item_id)
                   for _ in range(max(0, quantity))]
            created[(guild_id, user_id, item_id)] = ids
            self.db.execute('''DELETE FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id=?''', (guild_id, user_id, item_id))

        for guild_id, user_id, slot, item_id in legacy_rows:
            if not self._is_instance_item(item_id):
                continue
            candidates = created.get((guild_id, user_id, item_id), [])
            instance_id = candidates.pop(0) if candidates else self._insert_instance(guild_id, user_id, item_id)
            self.db.execute('INSERT OR REPLACE INTO rpg_equipment VALUES (?,?,?,?)',
                            (guild_id, user_id, slot, instance_id))
        if had_legacy_table:
            self.db.execute('DROP TABLE rpg_equipment_legacy')
        self.db.execute('''INSERT OR IGNORE INTO rpg_schema_migrations VALUES (?,?)''',
                        ('equipment_instances_v1', int(time.time())))

    def _materialize_legacy_inventory(self, guild_id, user_id):
        """Compatibility bridge for callers/tests still inserting old item rows."""
        rows = self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
            WHERE guild_id=? AND user_id=?''', (guild_id, user_id)).fetchall()
        for item_id, quantity in rows:
            if not self._is_instance_item(item_id):
                continue
            for _ in range(max(0, quantity)):
                self._insert_instance(guild_id, user_id, item_id)
            self.db.execute('''DELETE FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id=?''', (guild_id, user_id, item_id))

    def _materialize_legacy_inventory_atomic(self, guild_id, user_id):
        if self.db.in_transaction:
            self._materialize_legacy_inventory(guild_id, user_id)
        else:
            with self.db:
                self._materialize_legacy_inventory(guild_id, user_id)

    def _instance(self, guild_id, user_id, instance_id):
        row = self.db.execute('''SELECT instance_id,guild_id,user_id,item_id
            FROM rpg_equipment_instances WHERE instance_id=? AND guild_id=? AND user_id=?''',
            (instance_id, guild_id, user_id)).fetchone()
        if not row:
            return None
        sockets = tuple(self.db.execute('''SELECT socket_index,socket_item_id
            FROM rpg_instance_sockets WHERE instance_id=? ORDER BY socket_index''', (instance_id,)))
        affixes = tuple(self.db.execute('''SELECT affix_index,affix_id,effect_key,rolled_value
            FROM rpg_instance_affixes WHERE instance_id=? ORDER BY affix_index''', (instance_id,)))
        return EquipmentInstance(*row, sockets=sockets, affixes=affixes)

    def equipment_instances(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        self._materialize_legacy_inventory_atomic(guild_id, user_id)
        ids = [row[0] for row in self.db.execute('''SELECT instance_id
            FROM rpg_equipment_instances WHERE guild_id=? AND user_id=? ORDER BY instance_id''',
            (guild_id, user_id))]
        return [self._instance(guild_id, user_id, instance_id) for instance_id in ids]

    def inventory_entries(self, guild_id, user_id):
        """Return stackable rows plus one row for every equipment instance."""
        self.ensure_starter(guild_id, user_id)
        self._materialize_legacy_inventory_atomic(guild_id, user_id)
        entries = [InventoryEntry(key, key, ITEMS[key], quantity)
                   for key, quantity in self.db.execute('''SELECT item_id,quantity FROM rpg_inventory
                       WHERE guild_id=? AND user_id=?''', (guild_id, user_id)) if key in ITEMS]
        for instance in self.equipment_instances(guild_id, user_id):
            entries.append(InventoryEntry(instance.token, self.instance_item_id(instance),
                                          self.resolved_item(instance), 1, instance.instance_id))
        return sorted(entries, key=lambda entry: (entry.item.category, entry.item.name,
                                                   entry.instance_id or 0, entry.item_id))

    @staticmethod
    def instance_token(instance_id):
        return f'instance:{instance_id}'

    def instance_item_id(self, instance):
        sockets = dict(instance.sockets)
        paint = sockets.get(0)
        if paint in PAINT_ITEMS.values():
            color = next(color for color, key in PAINT_ITEMS.items() if key == paint)
            variant = f'{instance.item_id}:{color}'
            if variant in ITEMS:
                return variant
        return instance.item_id

    def resolved_item(self, instance):
        item = ITEMS[self.instance_item_id(instance)]
        stats, combat = list(item.stats), list(item.combat)
        changes = {}
        for _, _, effect_key, value in instance.affixes:
            if effect_key.startswith('stat:'):
                index = int(effect_key.split(':', 1)[1])
                stats[index] += value
            elif effect_key.startswith('combat:'):
                index = int(effect_key.split(':', 1)[1])
                combat[index] += value
            elif effect_key in ('speed', 'accuracy', 'evasion', 'lifesteal',
                                'damage_guard_chance', 'vulnerable_chance',
                                'vulnerable_percent', 'healing_share',
                                'alternating_damage_percent', 'defense_conversion'):
                changes[effect_key] = changes.get(effect_key, getattr(item, effect_key)) + value
        crystal_table = self.db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='rpg_crystal_instances'""").fetchone()
        if crystal_table:
            rows = self.db.execute('''SELECT effect_keys,rolled_values
                FROM rpg_crystal_instances WHERE equipment_instance_id=? ORDER BY socket_index''',
                (instance.instance_id,)).fetchall()
            for effects_json, values_json in rows:
                for effect_key, value in zip(json.loads(effects_json), json.loads(values_json)):
                    if effect_key in COMBAT_NAMES:
                        combat[COMBAT_NAMES.index(effect_key)] += value
                    elif effect_key in ('speed', 'accuracy', 'evasion', 'lifesteal',
                                        'critical_points', 'healing_received_percent'):
                        changes[effect_key] = changes.get(
                            effect_key, getattr(item, effect_key)) + value
        return replace(item, stats=tuple(stats), combat=tuple(combat), **changes)

    def _resolve_instance(self, guild_id, user_id, reference):
        if isinstance(reference, int) or isinstance(reference, str) and reference.startswith('instance:'):
            try:
                instance_id = int(reference.split(':', 1)[1]) if isinstance(reference, str) else reference
            except ValueError:
                return None
            return self._instance(guild_id, user_id, instance_id)
        if reference not in ITEMS or not self._is_instance_item(reference):
            return None
        self._materialize_legacy_inventory_atomic(guild_id, user_id)
        instances = self.equipment_instances(guild_id, user_id)
        matches = [instance for instance in instances if self.instance_item_id(instance) == reference]
        if not matches:
            return None
        equipped = {row[0] for row in self.db.execute('''SELECT instance_id FROM rpg_equipment
            WHERE guild_id=? AND user_id=?''', (guild_id, user_id))}
        return next((instance for instance in matches if instance.instance_id in equipped), matches[0])

    def item_for_reference(self, guild_id, user_id, reference):
        instance = self._resolve_instance(guild_id, user_id, reference)
        return self.resolved_item(instance) if instance else ITEMS.get(reference)

    def get_instance(self, guild_id, user_id, reference):
        return self._resolve_instance(guild_id, user_id, reference)

    def set_affixes(self, guild_id, user_id, reference, affixes):
        """Replace rolls on one item; intended for the future roll/reroll service.

        Each entry is ``(affix_id, effect_key, rolled_value)``. Values are saved,
        rather than regenerated from a seed, so balance changes cannot silently
        alter existing equipment.
        """
        allowed_fields = {'speed', 'accuracy', 'evasion', 'lifesteal',
                          'damage_guard_chance', 'vulnerable_chance',
                          'vulnerable_percent', 'healing_share',
                          'alternating_damage_percent', 'defense_conversion'}
        normalized = []
        for index, entry in enumerate(affixes):
            if len(entry) != 3:
                raise CharacterError('詞條資料格式錯誤。')
            affix_id, effect_key, value = entry
            if not isinstance(affix_id, str) or not affix_id or not isinstance(effect_key, str):
                raise CharacterError('詞條資料格式錯誤。')
            valid_vector = (effect_key.startswith('stat:') or effect_key.startswith('combat:'))
            if valid_vector:
                try:
                    vector, position = effect_key.split(':', 1)
                    limit = 5 if vector == 'stat' else 4
                    valid_vector = 0 <= int(position) < limit
                except ValueError:
                    valid_vector = False
            if effect_key not in allowed_fields and not valid_vector:
                raise CharacterError('詞條效果種類無效。')
            if type(value) is not int:
                raise CharacterError('詞條數值必須是整數。')
            normalized.append((index, affix_id, effect_key, value))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            instance = self._resolve_instance(guild_id, user_id, reference)
            if not instance:
                raise CharacterError('背包中沒有這件裝備。')
            self.db.execute('DELETE FROM rpg_instance_affixes WHERE instance_id=?',
                            (instance.instance_id,))
            self.db.executemany('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)',
                                [(instance.instance_id, *entry) for entry in normalized])
        return self._instance(guild_id, user_id, instance.instance_id)

    def job(self, guild_id, user_id):
        row = self.db.execute('SELECT job FROM rpg_characters WHERE guild_id=? AND user_id=?',
                              (guild_id, user_id)).fetchone()
        return row[0] if row else '民兵'

    def has_character(self, guild_id, user_id):
        return self.db.execute('SELECT 1 FROM rpg_starter_claims WHERE guild_id=? AND user_id=?',
                               (guild_id, user_id)).fetchone() is not None

    def inventory(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        self._materialize_legacy_inventory_atomic(guild_id, user_id)
        stackable = [row[0] for row in self.db.execute(
            'SELECT item_id FROM rpg_inventory WHERE guild_id=? AND user_id=? ORDER BY item_id',
            (guild_id, user_id)) if row[0] in ITEMS]
        equipment = [self.instance_item_id(instance)
                     for instance in self.equipment_instances(guild_id, user_id)]
        return sorted(set(stackable + equipment))

    def ensure_starter(self, guild_id, user_id):
        if self.db.execute('SELECT 1 FROM rpg_starter_claims WHERE guild_id=? AND user_id=?',
                           (guild_id, user_id)).fetchone():
            return
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.ensure_starter(guild_id, user_id)
            return
        granted = self.db.execute('INSERT OR IGNORE INTO rpg_starter_claims VALUES (?,?)',
                                  (guild_id, user_id))
        if granted.rowcount:
            instance_id = self._insert_instance(guild_id, user_id, 'starter:club')
            self.db.execute('INSERT OR IGNORE INTO rpg_equipment VALUES (?,?,?,?)',
                            (guild_id, user_id, '武器', instance_id))

    def create(self, guild_id, user_id):
        """Atomically create a formal player and grant their starter equipment."""
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                return self.create(guild_id, user_id)
        created = self.store.create_player(guild_id, user_id)
        self.ensure_starter(guild_id, user_id)
        return created

    def snapshot(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        level = level_for(self.store.xp(guild_id, user_id))
        job = self.job(guild_id, user_id)
        # Job is chosen explicitly at Lv.10; unchosen characters remain militia.
        stage = stage_for(level, self.settings) if job != '民兵' else 0
        capacity = 1 if job == '民兵' else stage + 2
        slots = ['武器', '套裝'] + [f'飾品{i}' for i in range(1, capacity + 1)]
        raw = dict(self.db.execute('SELECT slot, instance_id FROM rpg_equipment WHERE guild_id=? AND user_id=?',
                                   (guild_id, user_id)))
        equipped, equipped_instances, resolved = {}, {}, {}
        for slot, instance_id in raw.items():
            instance = self._instance(guild_id, user_id, instance_id)
            item = self.resolved_item(instance) if instance else None
            if (slot in slots and item and
                    (not item.job or item.job == job) and
                    level >= item_level(item, self.settings)):
                equipped[slot] = self.instance_item_id(instance)
                equipped_instances[slot] = instance_id
                resolved[slot] = item
        # First nine level-ups always use militia growth, even after changing jobs.
        growth = GROWTH[job]
        base = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                     + (stage * weight * 2 if job != '民兵' else 0) for weight in growth)
        bonus = tuple(sum(item.stats[i] for item in resolved.values()) for i in range(5))
        total = tuple(a + b for a, b in zip(base, bonus))
        combat = combat_from_stats(total, job)
        combat_bonus = {name: sum(item.combat[i] for item in resolved.values())
                        for i, name in enumerate(COMBAT_NAMES)}
        for name, value in combat_bonus.items():
            combat[name] += value
        weapon = resolved.get('武器')
        suit = resolved.get('套裝')
        active_set = weapon.set_id if weapon and suit and weapon.set_id == suit.set_id else ''
        set_bonus_text = ''
        if active_set:
            set_name, set_effect = SET_BONUSES[active_set]
            set_bonus_text = f'{set_name}（2/2）：{set_effect}'
            if active_set == 'furnace_heart':
                combat['HP'] = combat['HP'] * 105 // 100
            elif active_set == 'reverse_tide':
                combat['HP'] = combat['HP'] * 107 // 100
            elif active_set == 'spore_shadow':
                combat['命中率'] += 5
                combat['暴擊率'] = min(100, combat['暴擊率'] + 3)
            elif active_set == 'star_chaser':
                combat['命中率'] += 8
                combat['暴擊率'] = min(100, combat['暴擊率'] + 5)
            elif active_set == 'mist_prayer':
                combat['攻擊'] = combat['攻擊'] * 104 // 100
                combat['治療量'] = combat['治療量'] * 104 // 100
            elif active_set == 'tide_rite':
                combat['攻擊'] = combat['攻擊'] * 106 // 100
                combat['治療量'] = combat['治療量'] * 106 // 100
        combat['閃避率'] += sum(item.evasion for item in resolved.values())
        combat['命中率'] += sum(item.accuracy for item in resolved.values())
        combat['暴擊率'] = min(100, combat['暴擊率']
                           + sum(item.critical_points for item in resolved.values()))
        speed = max(1, min(100, BASE_SPEED.get(job, 50) + sum(item.speed for item in resolved.values())))
        combat['速度'] = speed
        stability = weapon.stability if weapon else (100, 100)
        stability_bonus = {'molten_vein': 15, 'starforged': 25}.get(active_set, 0)
        if stability_bonus:
            stability = (min(stability[1], stability[0] + stability_bonus), stability[1])
        crystal_effects = []
        crystal_table = self.db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='rpg_crystal_instances'""").fetchone()
        if crystal_table and equipped_instances:
            placeholders = ','.join('?' for _ in equipped_instances)
            rows = self.db.execute(f'''SELECT crystal_type,affix_id,effect_keys,rolled_values,job,
                equipment_instance_id FROM rpg_crystal_instances
                WHERE equipment_instance_id IN ({placeholders}) ORDER BY equipment_instance_id,socket_index''',
                tuple(equipped_instances.values())).fetchall()
            unique_affixes = set()
            additive = {'HP', '攻擊', '防禦', '治療量', 'accuracy', 'critical_points',
                        'evasion_points', 'speed', 'lifesteal_percent',
                        'healing_received_percent'}
            for crystal_type, affix_id, effects_json, values_json, crystal_job, equipment_id in rows:
                effects = tuple(json.loads(effects_json))
                values = tuple(json.loads(values_json))
                if crystal_type == 'source' and crystal_job != job:
                    raise CharacterError('已鑲嵌的源色結晶與目前職業不符。')
                if any(effect not in additive for effect in effects):
                    if affix_id in unique_affixes:
                        raise CharacterError('穿戴中的裝備含有重複的唯一結晶效果。')
                    unique_affixes.add(affix_id)
                crystal_effects.append(dict(
                    type=crystal_type, affix_id=affix_id, effects=effects, values=values,
                    job=crystal_job, equipment_instance_id=equipment_id))
        return dict(level=level, job=job, stage=stage, capacity=capacity, slots=slots,
                    title=job if job == '民兵' else PREFIXES[stage] + job,
                    base=base, bonus=bonus, total=total, combat=combat, equipped=equipped,
                    equipped_instances=equipped_instances,
                    combat_bonus=combat_bonus, stability=stability, speed=speed,
                    lifesteal=sum(item.lifesteal for item in resolved.values()),
                    healing_received_percent=sum(
                        item.healing_received_percent for item in resolved.values()),
                    damage_guard_chance=min(100, sum(item.damage_guard_chance for item in resolved.values())),
                    vulnerable_chance=weapon.vulnerable_chance if weapon else 0,
                    vulnerable_percent=weapon.vulnerable_percent if weapon else 0,
                    healing_share=max((item.healing_share for item in resolved.values()), default=0),
                    alternating_damage_percent=max(
                        (item.alternating_damage_percent for item in resolved.values()), default=0),
                    defense_conversion=any(item.defense_conversion for item in resolved.values()),
                    first_skill_cooldown_reduction=max(
                        (item.first_skill_cooldown_reduction for item in resolved.values()), default=0),
                    active_set=active_set, set_bonus_text=set_bonus_text,
                    critical_damage_percent=CRITICAL_DAMAGE_PERCENT[job],
                    crystal_effects=crystal_effects)

    def inventory_counts(self, guild_id, user_id):
        self.ensure_starter(guild_id, user_id)
        self._materialize_legacy_inventory_atomic(guild_id, user_id)
        counts = dict(self.db.execute('SELECT item_id, quantity FROM rpg_inventory WHERE guild_id=? AND user_id=?',
                                      (guild_id, user_id)))
        for instance in self.equipment_instances(guild_id, user_id):
            key = self.instance_item_id(instance)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def showcase(self, guild_id, user_id):
        row = self.db.execute('SELECT showcase_item_id,showcase_instance_id FROM rpg_character_profiles '
                              'WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
        if not row:
            return None
        if row[1]:
            instance = self._instance(guild_id, user_id, row[1])
            return self.instance_item_id(instance) if instance else None
        if not row[0] or row[0] not in ITEMS:
            return None
        owned = self.db.execute('SELECT 1 FROM rpg_inventory '
                                'WHERE guild_id=? AND user_id=? AND item_id=?',
                                (guild_id, user_id, row[0])).fetchone()
        if owned:
            return row[0]
        instance = self._resolve_instance(guild_id, user_id, row[0])
        return self.instance_item_id(instance) if instance else None

    def showcase_reference(self, guild_id, user_id):
        row = self.db.execute('SELECT showcase_item_id,showcase_instance_id FROM rpg_character_profiles '
                              'WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
        if not row:
            return None
        if row[1] and self._instance(guild_id, user_id, row[1]):
            return self.instance_token(row[1])
        return row[0]

    def showcase_item(self, guild_id, user_id):
        reference = self.showcase_reference(guild_id, user_id)
        return self.item_for_reference(guild_id, user_id, reference) if reference else None

    def set_showcase(self, guild_id, user_id, item_id):
        if item_id is not None and item_id not in ITEMS and not str(item_id).startswith('instance:'):
            raise CharacterError('找不到這件展示品。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.ensure_starter(guild_id, user_id)
            instance = self._resolve_instance(guild_id, user_id, item_id) if item_id is not None else None
            stackable = item_id is not None and self.db.execute(
                'SELECT 1 FROM rpg_inventory WHERE guild_id=? AND user_id=? AND item_id=?',
                (guild_id, user_id, item_id)).fetchone()
            if item_id is not None and not instance and not stackable:
                raise CharacterError('背包中沒有這件物品。')
            definition_id = None if instance else item_id
            instance_id = instance.instance_id if instance else None
            self.db.execute('''INSERT INTO rpg_character_profiles
                (guild_id,user_id,showcase_item_id,showcase_instance_id) VALUES (?, ?, ?, ?) '''
                            'ON CONFLICT(guild_id, user_id) DO UPDATE SET '
                            'showcase_item_id=excluded.showcase_item_id, '
                            'showcase_instance_id=excluded.showcase_instance_id',
                            (guild_id, user_id, definition_id, instance_id))
        return item_id

    def _grant(self, guild_id, user_id, job):
        candidates = [key for key, item in ITEMS.items()
                      if (key.startswith('accessory:') or
                          item.job == job and item.stage == 0 and item.slot in ('武器', '套裝'))]
        granted = []
        for key in candidates:
            if self.inventory_counts(guild_id, user_id).get(key, 0) == 0:
                self._insert_instance(guild_id, user_id, key)
                granted.append(key)
        return granted

    def change_job(self, guild_id, user_id, job):
        if job not in JOBS:
            raise CharacterError('請選擇裝甲步兵、騎士、弓兵或僧侶。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.ensure_starter(guild_id, user_id)
            level = level_for(self.store.xp(guild_id, user_id))
            if level < 10:
                raise CharacterError('目前是民兵，達到 Lv.10 才能轉職。')
            if self.job(guild_id, user_id) == job:
                raise CharacterError('你已經是這個職業；進階裝備請從 /冒險 → 商店 購買取得。')
            self.db.execute('INSERT INTO rpg_characters VALUES (?, ?, ?) '
                            'ON CONFLICT(guild_id, user_id) DO UPDATE SET job=excluded.job',
                            (guild_id, user_id, job))
            self._grant(guild_id, user_id, job)
            self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            for slot in ('武器', '套裝'):
                instance = self._resolve_instance(guild_id, user_id, f'{job}:0:{slot}')
                self.db.execute('INSERT INTO rpg_equipment VALUES (?, ?, ?, ?)',
                                (guild_id, user_id, slot, instance.instance_id))
        return self.snapshot(guild_id, user_id)

    def claim(self, guild_id, user_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.snapshot(guild_id, user_id)
            granted = []
            if self.inventory_counts(guild_id, user_id).get('starter:club', 0) == 0:
                self._insert_instance(guild_id, user_id, 'starter:club')
                granted.append('starter:club')
            if state['job'] != '民兵':
                granted.extend(self._grant(guild_id, user_id, state['job']))
            return granted

    def equip(self, guild_id, user_id, item_id, accessory_slot=1):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.snapshot(guild_id, user_id)
            instance = self._resolve_instance(guild_id, user_id, item_id)
            if not instance:
                raise CharacterError('背包中沒有這件裝備，請重新開啟 /冒險 → 裝備／能力 並從面板選擇。')
            item = self.resolved_item(instance)
            if item.job and item.job != state['job']:
                raise CharacterError('這件裝備不適合目前職業。')
            if state['level'] < item_level(item, self.settings):
                raise CharacterError('等級尚未達到這件裝備的需求。')
            slot = item.slot
            if slot == '飾品':
                if not 1 <= accessory_slot <= state['capacity']:
                    raise CharacterError(f'目前只有 {state["capacity"]} 個飾品格。')
                slot = f'飾品{accessory_slot}'
            crystal_table = self.db.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='rpg_crystal_instances'""").fetchone()
            if crystal_table:
                additive = {'HP', '攻擊', '防禦', '治療量', 'accuracy',
                            'critical_points', 'evasion_points', 'speed',
                            'lifesteal_percent', 'healing_received_percent'}

                def unique_affixes(instance_ids):
                    if not instance_ids:
                        return set()
                    placeholders = ','.join('?' for _ in instance_ids)
                    rows = self.db.execute(f'''SELECT affix_id,effect_keys
                        FROM rpg_crystal_instances
                        WHERE equipment_instance_id IN ({placeholders})''', tuple(instance_ids))
                    return {affix_id for affix_id, effects_json in rows
                            if any(effect not in additive for effect in json.loads(effects_json))}

                candidate = unique_affixes([instance.instance_id])
                other_ids = [row[0] for row in self.db.execute('''SELECT instance_id
                    FROM rpg_equipment WHERE guild_id=? AND user_id=? AND slot<>?''',
                    (guild_id, user_id, slot))]
                duplicates = candidate & unique_affixes(other_ids)
                if duplicates:
                    raise CharacterError(
                        '這件裝備與目前穿戴裝備含有重複的唯一結晶效果；請先更換或拆除其中一顆。')
            # Moving the same instance never duplicates its bonus. Accessories
            # retain the existing rule that the same definition cannot occupy
            # multiple accessory slots even when several copies are owned.
            self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=? AND instance_id=?',
                            (guild_id, user_id, instance.instance_id))
            if item.slot == '飾品':
                self.db.execute('''DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=?
                    AND instance_id IN (SELECT instance_id FROM rpg_equipment_instances
                    WHERE guild_id=? AND user_id=? AND item_id=?)''',
                    (guild_id, user_id, guild_id, user_id, instance.item_id))
            self.db.execute('INSERT INTO rpg_equipment VALUES (?, ?, ?, ?) '
                            'ON CONFLICT(guild_id, user_id, slot) DO UPDATE SET instance_id=excluded.instance_id',
                            (guild_id, user_id, slot, instance.instance_id))
        return slot

    def buy(self, guild_id, user_id, item_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            item = ITEMS.get(item_id)
            if not item or item.price <= 0:
                raise CharacterError('這件物品不在商店販售。')
            state = self.snapshot(guild_id, user_id)
            if item.job != state['job']:
                raise CharacterError('只能購買目前職業的裝備。')
            if state['level'] < stage_level(item.stage, self.settings):
                raise CharacterError('尚未達到這件裝備的等級需求。')
            if self.inventory_counts(guild_id, user_id).get(item_id, 0):
                raise CharacterError('你已經持有這件裝備，不需要重複購買。')
            paid = self.db.execute('UPDATE rpg_wallets SET gold=gold-? WHERE guild_id=? AND user_id=? AND gold>=?',
                                   (item.price, guild_id, user_id, item.price))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，需要 {item.price:,} 金幣。')
            self._insert_instance(guild_id, user_id, item_id)
        return item

    def exchange_balloon_painting(self, guild_id, user_id):
        """Exchange bound raid proofs for one transferable Balloon painting."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                WHERE guild_id=? AND user_id=? AND item_id='proof:raid' AND quantity>=?''',
                (BALLOON_PAINTING_PROOF_COST, guild_id, user_id,
                 BALLOON_PAINTING_PROOF_COST))
            if not paid.rowcount:
                raise CharacterError(
                    f'討伐之證不足，需要 {BALLOON_PAINTING_PROOF_COST} 個。')
            self.db.execute('''DELETE FROM rpg_inventory
                WHERE guild_id=? AND user_id=? AND item_id='proof:raid' AND quantity=0''',
                (guild_id, user_id))
            add_owned_item(self.db, guild_id, user_id, 'painting:balloon')
        return ITEMS['painting:balloon']

    def unequip(self, guild_id, user_id, slot):
        if slot not in ('武器', '套裝', '飾品1', '飾品2', '飾品3', '飾品4', '飾品5'):
            raise CharacterError('無效的裝備欄位。')
        with self.db:
            cursor = self.db.execute('DELETE FROM rpg_equipment WHERE guild_id=? AND user_id=? AND slot=?',
                                     (guild_id, user_id, slot))
            if not cursor.rowcount:
                raise CharacterError('這個欄位沒有裝備。')

    def available_quantity(self, guild, user, key):
        if isinstance(key, int) or isinstance(key, str) and key.startswith('instance:'):
            instance = self._resolve_instance(guild, user, key)
            if not instance:
                return 0
            equipped = self.db.execute('''SELECT 1 FROM rpg_equipment
                WHERE guild_id=? AND user_id=? AND instance_id=?''',
                (guild, user, instance.instance_id)).fetchone()
            return 0 if equipped else 1
        owned = self.inventory_counts(guild, user).get(key, 0)
        equipped_ids = [row[0] for row in self.db.execute('''SELECT instance_id FROM rpg_equipment
            WHERE guild_id=? AND user_id=?''', (guild, user))]
        equipped = sum(self.instance_item_id(instance) == key for instance_id in equipped_ids
                       if (instance := self._instance(guild, user, instance_id)))
        return max(0, owned - equipped)

    def combine_paint_set(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            counts = self.inventory_counts(guild, user)
            if any(counts.get(key, 0) < 1 for key in PAINT_ITEMS.values()):
                raise CharacterError('需要紅色、黃色、藍色噴漆罐各一罐才能組合。')
            for key in PAINT_ITEMS.values():
                self.db.execute('UPDATE rpg_inventory SET quantity=quantity-1 '
                                'WHERE guild_id=? AND user_id=? AND item_id=?', (guild, user, key))
                self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                                'AND item_id=? AND quantity=0', (guild, user, key))
            self.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                VALUES (?,?,?,1) ON CONFLICT(guild_id,user_id,item_id)
                DO UPDATE SET quantity=quantity+1''', (guild, user, 'paint:set'))
        return ITEMS['paint:set']

    def dye_equipment(self, guild, user, item_id, color):
        if color not in PAINT_ITEMS:
            raise CharacterError('請選擇紅色、黃色或藍色顏料。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            instance = self._resolve_instance(guild, user, item_id)
            item = ITEMS.get(instance.item_id) if instance else None
            if not item or not item.socket_base:
                raise CharacterError('這件裝備不能染色。')
            current_socket = dict(instance.sockets).get(0)
            if current_socket == PAINT_ITEMS[color]:
                raise CharacterError(f'這件裝備已經是{PAINT_NAMES[color]}。')
            counts = self.inventory_counts(guild, user)
            paint_key = PAINT_ITEMS[color]
            if counts.get(paint_key, 0) < 1:
                raise CharacterError(f'背包中沒有{ITEMS[paint_key].name}。')
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''', (DYE_PRICE, guild, user, DYE_PRICE))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，染色需要 {DYE_PRICE:,} 金幣。')
            self.db.execute('UPDATE rpg_inventory SET quantity=quantity-1 '
                            'WHERE guild_id=? AND user_id=? AND item_id=?', (guild, user, paint_key))
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, paint_key))
            self.db.execute('''INSERT INTO rpg_instance_sockets(instance_id,socket_index,socket_item_id)
                VALUES (?,?,?) ON CONFLICT(instance_id,socket_index)
                DO UPDATE SET socket_item_id=excluded.socket_item_id''',
                (instance.instance_id, 0, paint_key))
            updated = self._instance(guild, user, instance.instance_id)
        return updated.token if isinstance(item_id, int) or str(item_id).startswith('instance:') else self.instance_item_id(updated)

    def embroider_accessory(self, guild, user, item_id, embroidery_id):
        embroidery = EMBROIDERIES.get(embroidery_id)
        if embroidery is None:
            raise CharacterError('請選擇有效的刺繡圖樣。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            instance = self._resolve_instance(guild, user, item_id)
            item = ITEMS.get(instance.item_id) if instance else None
            if not item or item.slot != '飾品' or item.embroidery_slots < 1:
                raise CharacterError('這件飾品沒有刺繡格。')
            affix_id = f'embroidery:{embroidery_id}'
            current = next((affix for affix in instance.affixes if affix[0] == 0), None)
            if current and current[1] == affix_id:
                raise CharacterError(f'這件飾品已經具有{embroidery[0]}。')
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''',
                                   (EMBROIDERY_PRICE, guild, user, EMBROIDERY_PRICE))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，刺繡需要 {EMBROIDERY_PRICE:,} 金幣。')
            self.db.execute('''INSERT INTO rpg_instance_affixes
                (instance_id,affix_index,affix_id,effect_key,rolled_value)
                VALUES (?,?,?,?,?) ON CONFLICT(instance_id,affix_index) DO UPDATE SET
                affix_id=excluded.affix_id,effect_key=excluded.effect_key,rolled_value=excluded.rolled_value''',
                (instance.instance_id, 0, affix_id, embroidery[1], embroidery[2]))
            updated = self._instance(guild, user, instance.instance_id)
        return updated.token if isinstance(item_id, int) or str(item_id).startswith('instance:') else self.instance_item_id(updated)

    def consume_item(self, guild, user, key, quantity=1):
        if self._is_instance_item(key):
            raise CharacterError('裝備必須指定單件實例，不能作為堆疊物品消耗。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            counts = self.inventory_counts(guild, user)
            if quantity < 1 or counts.get(key, 0) < quantity:
                raise CharacterError('背包中的物品數量不足。')
            self.db.execute('UPDATE rpg_inventory SET quantity=quantity-? '
                            'WHERE guild_id=? AND user_id=? AND item_id=?',
                            (quantity, guild, user, key))
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? '
                            'AND item_id=? AND quantity=0', (guild, user, key))

    def grant_item(self, guild, user, key, quantity=1):
        if key not in ITEMS or quantity < 1:
            raise CharacterError('無效的物品。')
        with self.db:
            if self._is_instance_item(key):
                return add_owned_item(self.db, guild, user, key, quantity)
            add_owned_item(self.db, guild, user, key, quantity)

    def dispose(self, guild, user, key, quantity, recipient=None):
        """Transfer or sell only unequipped copies in one transaction."""
        reference_instance = self._resolve_instance(guild, user, key)
        display_key = self.instance_item_id(reference_instance) if reference_instance else key
        item = self.resolved_item(reference_instance) if reference_instance else ITEMS.get(key)
        if not item or recipient is not None and not item.transferable:
            raise CharacterError('這件物品為綁定物品，不能給予其他玩家。')
        if recipient is None and not item_sellable(item):
            raise CharacterError('這件物品不能賣出。')
        if type(quantity) is not int or quantity < 1:
            raise CharacterError('數量必須是正整數。')
        if recipient == user:
            raise CharacterError('不能給予自己。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if quantity > self.available_quantity(guild, user, key):
                raise CharacterError('可用數量不足；正在穿戴的那一件不能給予或賣出，請先卸下。')
            if reference_instance:
                equipped = {row[0] for row in self.db.execute('''SELECT instance_id FROM rpg_equipment
                    WHERE guild_id=? AND user_id=?''', (guild, user))}
                matches = [instance for instance in self.equipment_instances(guild, user)
                           if self.instance_item_id(instance) == display_key and instance.instance_id not in equipped]
                if str(key).startswith('instance:'):
                    matches = [instance for instance in matches
                               if instance.instance_id == reference_instance.instance_id]
                if len(matches) < quantity:
                    raise CharacterError('可用數量不足；正在穿戴的那一件不能給予或賣出，請先卸下。')
                selected = matches[:quantity]
                if recipient is not None:
                    self.db.executemany('''UPDATE rpg_equipment_instances SET user_id=?
                        WHERE instance_id=? AND guild_id=? AND user_id=?''',
                        [(recipient, instance.instance_id, guild, user) for instance in selected])
                    if self.db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                        AND name='rpg_crystal_instances'""").fetchone():
                        self.db.executemany('''UPDATE rpg_crystal_instances SET user_id=?
                            WHERE equipment_instance_id=?''',
                            [(recipient, instance.instance_id) for instance in selected])
                    return 0
                if self.db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                    AND name='rpg_crystal_instances'""").fetchone():
                    self.db.executemany('DELETE FROM rpg_crystal_instances WHERE equipment_instance_id=?',
                                        [(instance.instance_id,) for instance in selected])
                self.db.executemany('DELETE FROM rpg_instance_sockets WHERE instance_id=?',
                                    [(instance.instance_id,) for instance in selected])
                self.db.executemany('DELETE FROM rpg_instance_affixes WHERE instance_id=?',
                                    [(instance.instance_id,) for instance in selected])
                self.db.executemany('DELETE FROM rpg_equipment_instances WHERE instance_id=?',
                                    [(instance.instance_id,) for instance in selected])
                gold = item_sell_price(item) * quantity
                self.db.execute('INSERT INTO rpg_wallets VALUES (?,?,?) '
                                'ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold',
                                (guild, user, gold))
                return gold
            self.db.execute('UPDATE rpg_inventory SET quantity=quantity-? WHERE guild_id=? AND user_id=? AND item_id=?',
                            (quantity, guild, user, key))
            self.db.execute('DELETE FROM rpg_inventory WHERE guild_id=? AND user_id=? AND item_id=? AND quantity=0',
                            (guild, user, key))
            if recipient is not None:
                self.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity) VALUES (?,?,?,?) '
                                'ON CONFLICT(guild_id,user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity',
                                (guild, recipient, key, quantity))
                return 0
            gold = item_sell_price(item) * quantity
            self.db.execute('INSERT INTO rpg_wallets VALUES (?,?,?) '
                            'ON CONFLICT(guild_id,user_id) DO UPDATE SET gold=gold+excluded.gold', (guild, user, gold))
            return gold
