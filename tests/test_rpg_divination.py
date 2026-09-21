from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_battle import Battle, Fighter, Rule, SKILLS, dump_battle, load_battle, raid_battle
from core.rpg_character import CharacterError, Characters
from core.rpg_divination import CARDS, Divinations, card_rarity
from core.rpg_mag_affinity import fuel_capacity
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

    def test_complete_major_arcana_has_mixed_unique_effects(self):
        self.assertEqual(len(CARDS), 22)
        self.assertIn('hierophant', CARDS)
        self.assertEqual({card.category for card in CARDS.values()}, {'戰鬥', '生活', '特殊'})
        self.assertEqual(len({card.effect for card in CARDS.values()}), 22)
        self.assertEqual((card_rarity(CARDS['strength']), card_rarity(CARDS['empress']),
                          card_rarity(CARDS['wheel']), card_rarity(CARDS['world'])),
                         ('常見', '少見', '稀有', '傳說'))

    def test_high_priestess_bonus_lasts_but_summon_remains_spent(self):
        service = Divinations(self.store)
        with self.store.db:
            self.store.db.execute('''INSERT INTO rpg_divinations
                (guild_id,user_id,day,draws,card,summon_raid_id,expires_at,selected_at)
                VALUES (1,1,?,1,'high_priestess','raid-1',9999,10)''',
                (self.store.day_key(10),))
        summoned = service.prepare_for_raid('raid-1', 1, [1], now=11)[1]
        ordinary = service.prepare_for_raid('raid-2', 1, [1], now=11)[1]
        self.assertEqual((summoned['xp_percent'], ordinary['xp_percent']), (15, 15))
        service.clear_raid('raid-1')
        status = service.status(1, 1, now=11)
        self.assertEqual(status['card'], 'high_priestess')
        self.assertEqual(status['summon_raid_id'], 'raid-1')

    def test_draw_price_increases_overwrite_and_resets_next_day(self):
        service = Divinations(self.store, FixedCards(
            'strength', 'emperor', 'empress', 'world', 'moon', 'sun',
            'death', 'tower', 'star'))
        offer, cost = service.reveal(1, 1, now=0)
        service.choose(1, 1, offer[0], now=0)
        self.assertEqual((offer, cost, self.store.gold(1, 1)),
                         (('strength', 'emperor', 'empress'), 0, 5000))
        offer, cost = service.reveal(1, 1, now=1)
        service.choose(1, 1, offer[0], now=1)
        self.assertEqual((offer[0], cost, self.store.gold(1, 1)), ('world', 300, 4700))
        self.assertEqual((service.status(1, 1, now=1)['draws'], service.status(1, 1, now=1)['next_price']),
                         (2, 600))
        offer, cost = service.reveal(1, 1, now=86400)
        self.assertEqual((offer[0], cost, service.status(1, 1, now=86400)['draws']), ('death', 0, 1))

    def test_mag_affinity_awards_selection_and_resonance_up_to_three_per_day(self):
        service = Divinations(self.store, FixedCards(
            'strength', 'emperor', 'empress', 'world', 'moon', 'sun',
            'death', 'tower', 'star', 'fool', 'lovers', 'chariot'))
        offer, _ = service.reveal(1, 1, now=0)
        service.choose(1, 1, offer[0], now=0)
        service.resonate(1, 1, offer[0], now=1, activation=0)
        offer, _ = service.reveal(1, 1, now=2)
        service.choose(1, 1, offer[0], now=2)
        service.resonate(1, 1, offer[0], now=3, activation=2)
        offer, _ = service.reveal(1, 1, now=4)
        service.choose(1, 1, offer[0], now=4)
        service.resonate(1, 1, offer[0], now=5, activation=4)
        self.assertEqual(service.affinity_status(1, 1, now=5)['score'], 3)
        self.assertEqual(service.affinity_status(1, 1, now=5)['today'], 3)

        offer, _ = service.reveal(1, 1, now=86400)
        service.choose(1, 1, offer[0], now=86400)
        self.assertEqual(service.affinity_status(1, 1, now=86400)['score'], 4)

    def test_mag_affinity_fuel_capacity_milestones(self):
        Divinations(self.store)
        for score, expected in ((0, 1000), (25, 1250), (50, 1500),
                                (75, 1750), (100, 2000)):
            with self.store.db:
                self.store.db.execute('''INSERT INTO rpg_mag_affinity VALUES (1,1,?)
                    ON CONFLICT(guild_id,user_id) DO UPDATE SET score=excluded.score''', (score,))
            self.assertEqual(fuel_capacity(self.store.db, 1, 1), expected)

    def test_snapshot_effect_awards_affinity_on_completion_day(self):
        service = Divinations(self.store, FixedCards('star', 'moon', 'sun'))
        service.draw(1, 1, now=0)
        self.assertTrue(service.resonate(1, 1, 'star', now=0, activation=0,
                                          award_now=86400))
        self.assertEqual(service.affinity_status(1, 1, now=0)['today'], 1)
        self.assertEqual(service.affinity_status(1, 1, now=86400)['today'], 1)
        self.assertEqual(service.affinity_status(1, 1, now=86400)['score'], 2)

    def test_timed_card_survives_raid_and_expires(self):
        service = Divinations(self.store, FixedCards('death', 'strength', 'emperor'))
        service.draw(1, 1, now=0)
        snapshot = service.prepare_for_raid('raid-1', 1, [1], now=1)
        self.assertEqual(snapshot[1], {
            'id': 'death', 'name': '死神', 'activation': 0, 'xp_percent': 0})
        service.clear_raid('raid-1')
        self.assertEqual(service.status(1, 1, now=2)['card'], 'death')
        self.assertIsNone(service.status(1, 1, now=21601)['card'])

    def test_restart_releases_interrupted_high_priestess_reservation(self):
        service = Divinations(self.store, FixedCards('high_priestess', 'strength', 'emperor'))
        service.draw(1, 1)
        service.reserve_summon(1, 1)
        self.assertEqual(service.status(1, 1, now=0)['summon_raid_id'], ':reserved:')
        restarted = Divinations(self.store)
        self.assertIsNone(restarted.status(1, 1, now=0)['summon_raid_id'])

    def test_temperance_preserves_meal_charge(self):
        provisions = Provisions(self.store)
        with self.store.db:
            self.store.db.execute("INSERT INTO rpg_inventory VALUES (1,1,'fishing:pond:common',3)")
            self.store.db.execute("INSERT INTO rpg_inventory VALUES (1,1,'farming:potato',2)")
        meal = provisions.cook(1, 1, 9,
            ['fishing:pond:common'] * 3 + ['farming:potato'] * 2, now=1)
        first = provisions.prepare_for_raid('raid-1', 1, [1], preserve_users=[1], now=2)
        second = provisions.prepare_for_raid('raid-1', 1, [1], preserve_users=[1], now=2)
        self.assertEqual(first, second)
        count = self.store.db.execute(
            'SELECT remaining FROM rpg_meal_claims WHERE meal_id=? AND user_id=1',
            (meal['id'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_combat_cards_apply_and_survive_snapshot(self):
        battle = raid_battle([participant(1, 'strength'), participant(2, 'lovers')],
                             {'kind': '巨獸', 'name': '巨獸', 'strength': 1}, seed=7)
        strong, lover, enemy = battle.fighters
        self.assertEqual(strong.stats['攻擊'], 108)
        self.assertEqual((lover.linked_user_id, strong.linked_user_id), (1, 2))
        before = (lover.hp, strong.hp)
        battle.hit(enemy, lover, precise=True)
        self.assertLess(lover.hp, before[0])
        self.assertLess(strong.hp, before[1])

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

    def test_settlement_keeps_timed_card_and_adds_resonance(self):
        divinations = Divinations(self.store)
        repo = RaidStore(self.store)
        policy = dict(victory_xp=100, victory_gold=0, drop_chance=1.0)
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
            self.store.db.execute('''INSERT OR REPLACE INTO rpg_divinations
                (guild_id,user_id,day,draws,card,expires_at,selected_at)
                VALUES (1,1,'1970-01-01',1,'wheel',999999,0)''')
        repo.save(raid)
        settled = repo.settle(raid['id'], battle_data, SimpleNamespace(**policy))
        self.assertEqual(settled['rewards'][0]['xp'], 110)
        self.assertIsNotNone(settled['rewards'][0]['item'])
        self.assertEqual(self.store.xp(1, 1), 110)
        self.assertEqual(divinations.status(1, 1, now=0)['card'], 'wheel')
        self.assertEqual(divinations.status(1, 1, now=0)['draws'], 1)
        self.assertEqual(divinations.mastery(1, 1)['wheel'], 1)
        self.assertEqual(divinations.affinity_status(1, 1)['score'], 1)
        repo.settle(raid['id'], battle_data, SimpleNamespace(**policy))
        self.assertEqual(divinations.affinity_status(1, 1)['score'], 1)

    def test_settlement_stacks_drink_with_meal_xp_gold_and_drop_effects(self):
        repo = RaidStore(self.store)
        policy = dict(victory_xp=100, victory_gold=100, drop_chance=0.0)
        raid = repo.create(1, 10, {'kind': '巨獸', 'strength': 1}, 0, policy)
        raid.update(status='running', seed=31,
                    participants=[dict(id=1, state={'job': '民兵', 'level': 1},
                                       tavern={'xp_percent': 5},
                                       meal={'xp_percent': 15, 'gold_percent': 15,
                                             'drop_points': 5})])
        player = Fighter('玩家', 0, '民兵',
            {'HP': 100, '攻擊': 10, '防禦': 0, '治療量': 0,
             '命中率': 100, '閃避率': 0, '暴擊率': 0}, 10, [], user_id=1)
        enemy = Fighter('敵人', 1, '巨獸',
            {'HP': 100, '攻擊': 10, '防禦': 0, '治療量': 0,
             '命中率': 100, '閃避率': 0, '暴擊率': 0}, 1, [])
        enemy.hp = 0
        battle_data = dict(result='勝利', round=1,
                           fighters=[asdict(player), asdict(enemy)])
        repo.save(raid)

        settled = repo.settle(raid['id'], battle_data, SimpleNamespace(**policy))

        reward = settled['rewards'][0]
        self.assertEqual((reward['xp'], reward['gold']), (120, 115))
        self.assertIsNotNone(reward['item'])
        self.assertEqual((self.store.xp(1, 1), self.store.gold(1, 1)), (120, 5115))


if __name__ == '__main__':
    unittest.main()
