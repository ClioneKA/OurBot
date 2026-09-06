from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_battle import Battle, Fighter, Rule, SKILLS, dump_battle, load_battle, raid_battle
from core.rpg_character import CharacterError, Characters
from core.rpg_divination import CARDS, Divinations
from core.rpg_provisions import Provisions
from core.rpg_raid_store import RaidStore
from core.settings import RPGSettings


class FixedCards:
    def __init__(self, *cards):
        self.cards = list(cards)

    def choices(self, population, weights, k):
        return [self.cards.pop(0)]


def participant(user_id, card=None):
    data = dict(
        id=user_id, name=f'玩家{user_id}',
        state=dict(level=30, job='民兵', speed=50,
                   combat={'HP': 1000, '攻擊': 100, '防禦': 50, '治療量': 100,
                           '命中率': 100, '閃避率': 10, '暴擊率': 10},
                   equipped={'武器': 'starter:club'}, stability=(100, 100)),
        rules=[])
    if card:
        data['fortune'] = dict(id=card, name=CARDS[card].name, xp_percent=10)
    return data


class DivinationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,5000)')

    def test_draw_price_increases_overwrite_and_resets_next_day(self):
        service = Divinations(self.store, FixedCards('strength', 'world', 'moon'))
        first, cost = service.draw(1, 1, now=0)
        self.assertEqual((first, cost, self.store.gold(1, 1)), ('strength', 300, 4700))
        second, cost = service.draw(1, 1, now=1)
        self.assertEqual((second, cost, self.store.gold(1, 1)), ('world', 600, 4100))
        self.assertEqual((service.status(1, 1, now=1)['draws'], service.status(1, 1, now=1)['next_price']),
                         (2, 900))
        third, cost = service.draw(1, 1, now=86400)
        self.assertEqual((third, cost, service.status(1, 1, now=86400)['draws']), ('moon', 300, 1))

    def test_binding_blocks_redraw_and_completion_only_clears_effect(self):
        service = Divinations(self.store, FixedCards('death'))
        service.draw(1, 1, now=0)
        snapshot = service.prepare_for_raid('raid-1', 1, [1])
        self.assertEqual(snapshot[1], {'id': 'death', 'name': '死神', 'xp_percent': 10})
        self.assertEqual(service.prepare_for_raid('raid-1', 1, [1]), snapshot)
        with self.assertRaisesRegex(CharacterError, '正在進行'):
            service.draw(1, 1, now=2)
        service.clear_raid('raid-1')
        status = service.status(1, 1, now=2)
        self.assertIsNone(status['card'])
        self.assertEqual((status['draws'], status['next_price']), (1, 600))

    def test_restart_releases_interrupted_high_priestess_reservation(self):
        service = Divinations(self.store, FixedCards('high_priestess'))
        service.draw(1, 1, now=0)
        service.reserve_summon(1, 1)
        self.assertEqual(service.status(1, 1, now=0)['summon_raid_id'], ':reserved:')
        restarted = Divinations(self.store)
        self.assertIsNone(restarted.status(1, 1, now=0)['summon_raid_id'])

    def test_temperance_preserves_selected_provisions(self):
        provisions = Provisions(self.store)
        with self.store.db:
            self.store.db.execute("INSERT INTO rpg_inventory VALUES (1,1,'food:pond:common',1)")
        provisions.select(1, 1, 'food', 'food:pond:common')
        first = provisions.prepare_for_raid('raid-1', 1, [1], preserve_users=[1])
        second = provisions.prepare_for_raid('raid-1', 1, [1], preserve_users=[1])
        self.assertEqual(first, second)
        count = self.store.db.execute("SELECT quantity FROM rpg_inventory WHERE item_id='food:pond:common'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(provisions.loadout(1, 1)['food'], 'food:pond:common')

    def test_combat_cards_apply_and_survive_snapshot(self):
        battle = raid_battle([participant(1, 'world'), participant(2, 'lovers')],
                             {'kind': '巨獸', 'name': '巨獸', 'strength': 1}, seed=7)
        world, lover, enemy = battle.fighters
        self.assertEqual((world.stats['HP'], world.stats['攻擊'], world.speed), (1100, 110, 55))
        self.assertEqual((lover.linked_user_id, world.linked_user_id), (1, 2))
        before = (lover.hp, world.hp)
        battle.hit(enemy, lover, precise=True)
        self.assertLess(lover.hp, before[0])
        self.assertLess(world.hp, before[1])

        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(restored.fighters[1].linked_user_id, 1)

    def test_death_judgement_and_magician_mechanics(self):
        attacker = Fighter('玩家', 0, '民兵',
            {'HP': 100, '攻擊': 100, '防禦': 0, '治療量': 0, '命中率': 100, '閃避率': 0, '暴擊率': 0},
            50, [], lifesteal=5, fortune_card='death', cooldown_reduction=1, user_id=1)
        enemy = Fighter('敵人', 1, '巨獸',
            {'HP': 500, '攻擊': 1000, '防禦': 0, '治療量': 0, '命中率': 100, '閃避率': 0, '暴擊率': 0},
            10, [])
        attacker.hp = 50
        battle = Battle([attacker, enemy], seed=1)
        battle.round = 1
        battle.hit(attacker, enemy, precise=True)
        self.assertEqual(attacker.hp, 55)
        rule = Rule(1, 1, True, 'always', 'lowest')
        battle.use_skill(attacker, rule, SKILLS['民兵'][0], enemy)
        self.assertEqual(attacker.ready[1], 3)

        attacker.fortune_card = 'judgement'
        attacker.hp = 20
        battle.hit(enemy, attacker, precise=True)
        self.assertEqual(attacker.hp, 1)
        battle.hit(enemy, attacker, precise=True)
        self.assertEqual(attacker.hp, 0)

    def test_settlement_adds_xp_drop_bonus_and_clears_card(self):
        divinations = Divinations(self.store)
        repo = RaidStore(self.store)
        policy = dict(victory_xp=100, victory_gold=0, drop_chance=0.0)
        raid = repo.create(1, 10, {'kind': '巨獸', 'strength': 1}, 0, policy)
        raid.update(status='running', seed=31,
                    participants=[dict(id=1, state={'job': '民兵', 'level': 1},
                                       fortune={'id': 'wheel', 'name': '命運之輪', 'xp_percent': 10})])
        player = Fighter('玩家', 0, '民兵',
            {'HP': 100, '攻擊': 10, '防禦': 0, '治療量': 0, '命中率': 100, '閃避率': 0, '暴擊率': 0}, 10, [], user_id=1)
        enemy = Fighter('敵人', 1, '巨獸',
            {'HP': 100, '攻擊': 10, '防禦': 0, '治療量': 0, '命中率': 100, '閃避率': 0, '暴擊率': 0}, 1, [])
        enemy.hp = 0
        battle_data = dict(result='勝利', round=1, fighters=[asdict(player), asdict(enemy)])
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO rpg_divinations VALUES (1,1,'1970-01-01',1,'wheel',?,NULL)",
                                  (raid['id'],))
        repo.save(raid)
        settled = repo.settle(raid['id'], battle_data, SimpleNamespace(**policy))
        self.assertEqual(settled['rewards'][0]['xp'], 110)
        self.assertIsNotNone(settled['rewards'][0]['item'])
        self.assertEqual(self.store.xp(1, 1), 110)
        self.assertIsNone(divinations.status(1, 1, now=0)['card'])
        self.assertEqual(divinations.status(1, 1, now=0)['draws'], 1)


if __name__ == '__main__':
    unittest.main()
