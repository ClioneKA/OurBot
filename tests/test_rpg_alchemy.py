from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_alchemy import (AlchemyDolls, BODY_BUDGETS, CORE_ITEM, POWDER_ITEM,
                              RARITY_ORDER, material_profile, parse_stone, stat_caps,
                              stone_id)
from core.rpg_character import Characters, CharacterError
from core.rpg_farming import Farming
from core.rpg_fishing import Fishing
from core.rpg_battle import raid_battle
from core.rpg_monsters import prepare_monster
from core.settings import RPGSettings


class FixedRng:
    def __init__(self, value=.1):
        self.value = value

    def random(self):
        return self.value

    def randrange(self, stop):
        return 1234

    def choice(self, values):
        return tuple(values)[0]

    def choices(self, values, weights=None, k=1):
        return [tuple(values)[0]] * k


class AlchemyDollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.characters.create(1, 10)
        self.alchemy = AlchemyDolls(self.store, RPGSettings(), FixedRng())

    def inventory(self):
        return self.characters.inventory_counts(1, 10)

    def make_body(self):
        self.characters.grant_item(1, 10, 'farming:wheat', 10)
        craft = self.alchemy.start_body(1, 10, ['farming:wheat'] * 10, now=100)
        body = self.alchemy.finish_body(1, 10, now=craft['ready_at'])
        self.alchemy.install_candidate(1, 10)
        return body

    def test_body_consumes_ten_materials_and_obeys_budget_and_caps(self):
        self.assertEqual(material_profile('farming:wheat')[0], 10)
        body = self.make_body()
        self.assertEqual(sum(body['stats']), BODY_BUDGETS[10])
        self.assertTrue(all(value <= cap for value, cap in zip(
            body['stats'], stat_caps(10, RPGSettings()))))
        self.assertNotIn('farming:wheat', self.inventory())
        with self.assertRaisesRegex(CharacterError, '沒有正在製作'):
            self.alchemy.finish_body(1, 10, now=999999)

    def test_core_orientation_engraving_and_participant_snapshot(self):
        body = self.make_body()
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '戰鬥')
        skill_item = stone_id('combat', 'power_strike', '傳說')
        self.characters.grant_item(1, 10, skill_item, 2)
        skills = self.alchemy.engrave(1, 10, core_id, 'combat', 1, skill_item)
        self.assertEqual(skills['combat'][0], {'key': 'power_strike', 'rarity': '傳說'})
        with self.assertRaisesRegex(CharacterError, '不能重複'):
            self.alchemy.engrave(1, 10, core_id, 'combat', 2, skill_item)
        self.alchemy.set_registered(1, 10, True)
        participant = self.alchemy.participant(1, 10, '測試者')
        self.assertTrue(participant['is_doll'])
        self.assertEqual(participant['owner_id'], 10)
        self.assertEqual(participant['state']['level'], body['tier'])
        self.assertEqual(len(participant['rules']), 3)
        self.assertIsNotNone(participant['rules'][0]['skill_id'])
        self.assertIsNone(participant['rules'][1]['skill_id'])

        human = dict(participant)
        human.update(id=99, is_doll=False, name='同能力真人')
        monster = prepare_monster({'kind': '巨獸', 'name': '測試怪物'}, '普通')
        two_humans = raid_battle([human, dict(human, id=98)], monster, 1)
        human_and_doll = raid_battle([human, participant], monster, 1)
        self.assertEqual(two_humans.fighters[-1].stats['HP'],
                         human_and_doll.fighters[-1].stats['HP'])

    def test_gacha_pity_ten_pull_guarantee_and_powder(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,10,10000)')
        results = self.alchemy.draw(1, 10, 'life', 10)
        self.assertEqual(len(results), 10)
        self.assertTrue(any(RARITY_ORDER.index(row['rarity']) >= 1 for row in results))
        item_id = results[0]['item_id']
        self.assertEqual(parse_stone(item_id)[:2], ('life', results[0]['skill']))
        powder = self.alchemy.decompose(1, 10, item_id)
        self.assertEqual(powder, 1)
        self.assertEqual(self.inventory()[POWDER_ITEM], 1)

    def test_fuel_conversion_uses_sell_value(self):
        self.characters.grant_item(1, 10, 'farming:wheat', 2)
        gained = self.alchemy.convert_fuel(1, 10, 'farming:wheat', 1)
        self.assertGreater(gained, 0)
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], gained)
        self.assertEqual(self.inventory()['farming:wheat'], 1)

    def test_life_automation_claims_and_repeats_once_with_fuel(self):
        self.make_body()
        body = self.alchemy.state(1, 10)['active_body']
        body['stats'] = [30, 30, 30, 30, 20]
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=? '
                                  'WHERE guild_id=1 AND user_id=10', (__import__('json').dumps(body),))
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '生活')
        for slot, key in enumerate(('fishing', 'farming'), 1):
            item_id = stone_id('life', key, '普通')
            self.characters.grant_item(1, 10, item_id)
            self.alchemy.engrave(1, 10, core_id, 'life', slot, item_id)
            self.alchemy.configure_life(1, 10, key, enabled=True, repeat=True)
        signup_item = stone_id('life', 'raid_signup', '普通')
        self.characters.grant_item(1, 10, signup_item)
        self.alchemy.engrave(1, 10, core_id, 'life', 3, signup_item)
        self.alchemy.configure_life(1, 10, 'raid_signup', enabled=True,
                                    pools=('regular',), qualities=('普通',))
        raid = {'id': 'r1', 'guild_id': 1, 'source': None, 'pool': 'regular',
                'monster': {'quality': '普通'}}
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [10])
        with self.store.db:
            for index in range(4):
                self.store.db.execute('''INSERT INTO rpg_alchemy_operations
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (1, 10, 'raid_signup', f'old-{index}', 'completed', 100, '{}', index))
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [10])
        raid['pool'] = 'mid'
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [])
        self.characters.grant_item(1, 10, 'farming:wheat', 2)
        self.alchemy.convert_fuel(1, 10, 'farming:wheat', 2)

        fishing = Fishing(self.store, rng=FixedRng(.99), boss_rng=FixedRng(.99),
                          material_rng=FixedRng(.99))
        farming = Farming(self.store, rng=FixedRng(.99), material_rng=FixedRng(.99),
                          specialization_rng=FixedRng(.99))
        fish = fishing.start(1, 10, 'pond', 'short', now=0)
        crop = farming.plant(1, 10, 'courtyard', 'potato', now=0)
        fish_result = self.alchemy.auto_fish(
            fishing, 1, 10, 'pond', 'short', 0, now=fish['ready_at'])
        farm_result = self.alchemy.auto_farm(
            farming, 1, 10, 'courtyard', 'potato', 0, now=crop['ready_at'])
        self.assertTrue(fish_result['restarted'])
        self.assertTrue(farm_result['replanted'])
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 24)
        self.assertEqual(fishing.state(1, 10)['session']['status'], 'active')
        self.assertEqual(farming.state(1, 10)['sessions']['courtyard']['status'], 'active')


if __name__ == '__main__':
    unittest.main()
