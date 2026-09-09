"""Resource-free equipment trials using the normal combat engine."""
from copy import deepcopy

from core.rpg_battle import Battle, Fighter, participant_fighters
from core.rpg_character import CharacterError


ROUND_OPTIONS = (5, 10, 20, 30)
COUNT_OPTIONS = (1, 3)
DEFENSE_OPTIONS = (0, 100, 300, 1000)
TRAINING_SEED = 1


class TrainingBattle(Battle):
    def act(self, actor):
        if actor.team == 0:
            super().act(actor)

    def apply_damage(self, target, damage, share_link=True, direct=False):
        if target.team == 1:
            # Keep all damage measurable, even hits exceeding the dummy's HP.
            target.hp = max(target.hp, int(damage) + 1)
        result = super().apply_damage(target, damage, share_link, direct)
        if target.team == 1:
            target.hp = target.stats['HP']
        return result


def train(participant, *, rounds=10, count=1, defense=0, seed=TRAINING_SEED):
    if (type(rounds) is not int or rounds not in ROUND_OPTIONS
            or type(count) is not int or count not in COUNT_OPTIONS
            or type(defense) is not int or defense not in DEFENSE_OPTIONS):
        raise CharacterError('無效的訓練假人設定。')
    fighters, badge_logs, passive_logs = participant_fighters([deepcopy(participant)])
    for index in range(count):
        fighters.append(Fighter(
            f'訓練假人 {index + 1}', 1, '訓練假人',
            {'HP': 1_000_000, '攻擊': 0, '防禦': defense, '治療量': 0,
             '命中率': 100, '閃避率': 0, '暴擊率': 0},
            1, [], is_boss=index == 0))
    battle = TrainingBattle(fighters, seed=seed, max_rounds=rounds)
    battle.log.extend(badge_logs + passive_logs)
    damage_by_round = []
    while battle.result is None:
        before = fighters[0].combat_stats['damage_dealt']
        battle.step()
        damage_by_round.append(fighters[0].combat_stats['damage_dealt'] - before)
    battle.result = '訓練完成'
    return battle, damage_by_round
