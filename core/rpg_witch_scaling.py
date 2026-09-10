"""Full-gear mechanic-aware reference curve and partial party-size scaling.

Calibrated for four-player parties, including skill/equipment unlock boundaries.
The scripts/simulate_witch_full_gear.py and scripts/calibrate_witch_party_size.py
tools generate local validation data; no report files are needed at runtime.
Factors multiply raw catalog profiles once, when the encounter starts.
"""
from math import sqrt

SCALING_VERSION = 2
REFERENCE_PARTY_SIZE = 4
# (average level, HP factor, attack factor, defense factor)
LEVEL_SCALES = (
    (10, 0.11590835089560506, 0.11797849389141321, 0.16558335842229296),
    (19, 0.19884763813005926, 0.1911109050445104, 0.28406805447151323),
    (20, 0.2834312825193028, 0.30704515949554895, 0.4049018321704325),
    (29, 0.3730807889667721, 0.3857103115727003, 0.5329725556668173),
    (30, 0.3942843417522129, 0.44795224696309344, 0.5632633453603042),
    (39, 0.4880556418070468, 0.5299193249258161, 0.6972223454386384),
    (40, 0.5073776641879717, 0.565750779511313, 0.7248252345542454),
    (44, 0.5507598887465869, 0.6068657270029674, 0.7867998410665528),
    (45, 0.597436773462267, 0.6389660318527448, 0.8534811049460956),
    (49, 0.640889026891275, 0.664779302670623, 0.9155557527018214),
    (50, 0.7, 0.8419189453125, 1.0),
    (59, 0.7988325375650149, 0.9446995548961424, 1.1411893393785928),
    (60, 0.8762732371741934, 1.0646143157687318, 1.2518189102488477),
    (89, 1.2017760152062689, 1.413730990356083, 1.7168228788660984),
    (90, 1.2406248613815316, 1.5108701621035792, 1.7723212305450453),
    (120, 1.5782175293021747, 1.9101852643383714, 2.254596470431678),
)


def level_scales(average_level):
    level = max(1, min(120, average_level))
    if level < 10:
        # Before profession selection, initial attributes grow by two per level.
        # This extrapolation is not part of the four-profession calibration.
        attribute = 10 + 2 * (level - 1)
        _, hp, attack, defense = LEVEL_SCALES[0]
        return hp * attribute / 28, attack * (50 + attribute * 10) / 330, defense * attribute / 28
    for lower, upper in zip(LEVEL_SCALES, LEVEL_SCALES[1:]):
        if level <= upper[0]:
            fraction = (level - lower[0]) / (upper[0] - lower[0])
            return tuple(a + (b - a) * fraction for a, b in zip(lower[1:], upper[1:]))
    return LEVEL_SCALES[-1][1:]


def witch_stat_scales(average_level, party_size):
    if party_size < 1:
        raise ValueError('Witch encounters require at least one participant.')
    hp, attack, defense = level_scales(average_level)
    ratio = party_size / REFERENCE_PARTY_SIZE
    return {'HP': hp * ratio, '攻擊': attack * sqrt(ratio), '防禦': defense}
