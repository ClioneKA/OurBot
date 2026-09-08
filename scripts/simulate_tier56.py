"""Deterministic balance sample for the tier-five and tier-six raid profiles."""
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Rule, raid_battle
from core.rpg_character import Characters
from core.rpg_monsters import prepare_monster
from core.settings import RPGSettings


SETS = {
    5: {
        '裝甲步兵': ('forge:infantry:weapon', 'forge:infantry:suit'),
        '騎士': ('forge:knight:weapon', 'forge:knight:suit'),
        '弓兵': ('fungus:archer:weapon', 'fungus:archer:suit'),
        '僧侶': ('fungus:monk:weapon', 'fungus:monk:suit'),
    },
    6: {
        '裝甲步兵': ('star:infantry:weapon', 'star:infantry:suit'),
        '騎士': ('tide:knight:weapon', 'tide:knight:suit'),
        '弓兵': ('star:archer:weapon', 'star:archer:suit'),
        '僧侶': ('tide:monk:weapon', 'tide:monk:suit'),
    },
}


def rule(slot, priority, condition, target, skill_id, value=None):
    return Rule(slot, priority, True, condition, target, skill_id, value)


def tactics(kind, job):
    support = {
        '騎士': [rule(1, 1, 'enemy_charging', 'boss', 4),
                 rule(2, 2, 'ally50', 'lowest', 2, 50), rule(3, 3, 'always', 'lowest', 3)],
        '僧侶': [rule(1, 1, 'ally_debuff_stacks' if kind == '逆潮聖骸' else 'ally_debuff',
                      'debuffed', 3, 2 if kind == '逆潮聖骸' else None),
                 rule(2, 2, 'ally50', 'lowest', 4, 50), rule(3, 3, 'ally50', 'lowest', 1, 70)],
    }
    if job in support:
        result = support[job]
        if job == '騎士' and kind in ('迷霧菌后', '逆潮聖骸'):
            result[0] = rule(1, 1, 'mechanic_target', 'mechanic', 4)
        return result
    if job == '裝甲步兵':
        return [rule(1, 1, 'enemy_guard' if kind == '熔爐鎧獸' else 'mechanic_target',
                     'boss' if kind == '熔爐鎧獸' else 'mechanic', 5),
                rule(2, 2, 'enemy_add' if kind == '迷霧菌后' else 'enemy_broken',
                     'add' if kind == '迷霧菌后' else 'boss', 4 if kind == '迷霧菌后' else 2),
                rule(3, 3, 'always', 'lowest', 1)]
    return [rule(1, 1, 'enemy_guard' if kind == '熔爐鎧獸' else 'mechanic_target',
                 'boss' if kind == '熔爐鎧獸' else 'mechanic', 4),
            rule(2, 2, 'enemies3' if kind == '迷霧菌后' else 'always',
                 'add' if kind == '迷霧菌后' else 'lowest', 3 if kind == '迷霧菌后' else 1,
                 3 if kind == '迷霧菌后' else None),
            rule(3, 3, 'always', 'lowest', 5)]


def simulate(samples=400):
    encounters = (('熔爐鎧獸', 5, 50), ('迷霧菌后', 5, 50),
                  ('星蝕巨神', 6, 60), ('逆潮聖骸', 6, 60))
    with tempfile.TemporaryDirectory() as directory:
        store = RPGStore(Path(directory) / 'rpg.db')
        characters = Characters(store, RPGSettings())
        parties = {}
        for tier, level in ((5, 50), (6, 60)):
            party = []
            for user_id, job in enumerate(('裝甲步兵', '騎士', '弓兵', '僧侶'), 1):
                store.award_voice([(tier, user_id, level_floor(level))])
                characters.change_job(tier, user_id, job)
                for item_id in SETS[tier][job]:
                    characters.grant_item(tier, user_id, item_id)
                    characters.equip(tier, user_id, item_id)
                party.append((user_id, job, characters.snapshot(tier, user_id)))
            parties[tier] = party
        for kind, tier, _ in encounters:
            wins, rounds = 0, 0
            for seed in range(samples):
                participants = [dict(id=user_id, name=job, state=state, passive_id=1,
                                     rules=[asdict(item) for item in tactics(kind, job)])
                                for user_id, job, state in parties[tier]]
                battle = raid_battle(participants, prepare_monster(
                    {'kind': kind, 'name': kind}, '普通'), seed)
                while battle.result is None:
                    battle.step()
                wins += battle.result == '勝利'
                rounds += battle.round
            print(f'{kind}: victory={wins / samples:.1%}, rounds={rounds / samples:.2f}')
        store.close()


if __name__ == '__main__':
    simulate()
