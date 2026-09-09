"""Fishing encounter catalog shared by fishing, raids and cooking."""
from dataclasses import dataclass


BOSS_CHANCE_PER_CATCH = 0.001


@dataclass(frozen=True)
class FishingBoss:
    name: str
    kind: str
    ingredient_name: str
    quality: int
    description: str


FISHING_BOSSES = {
    'pond': FishingBoss('許願池霸王蟹', '巨獸', '霸王蟹膏', 3,
                       '釣線牽動池底的銅幣，一隻巨蟹揮舞雙鉗爬上岸！'),
    'lake': FishingBoss('月露龍蝦王', '鐵殼魔像', '月露龍蝦尾', 4,
                       '月光映亮湖底，一隻披著厚甲的巨型龍蝦夾住魚鉤，揮舞雙螯爬上岸。'),
    'waterway': FishingBoss('幽淵鐘鰻', '深淵鐘龍', '鐘鰻精肉', 5,
                           '地下水路響起鐘鳴，纏住魚鉤的巨鰻披著層層鐘甲。'),
    'bay': FishingBoss('星潮帝王魷', '吞城鯨', '星潮厚切魷', 5,
                     '收竿時海面突然倒流，泛著星光的巨魷伸出腕足，捲起浪潮追逐魚餌。'),
}


def boss_ingredient(spot_id):
    return f'fishing:boss:{spot_id}'


def encounter_notice(result):
    encounter = result.get('boss_encounter')
    if not encounter:
        return ''
    return (f'\n🎣 釣出了【{encounter["name"]}】！遭遇已保存，'
            '將在特殊討伐頻道發布；頻道忙碌或未設定時會保留排隊。')
