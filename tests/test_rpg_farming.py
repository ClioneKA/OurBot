from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_character import CharacterError, Characters
from core.rpg_farming import Farming, PLANTS
from core.settings import RPGSettings


class FixedRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class FarmingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.farming = Farming(self.store, FixedRandom(0.99))

    def set_level(self, level):
        self.farming.state(1, 1)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_farming_players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(level),))

    def test_unlock_order_and_two_independent_plots(self):
        self.assertEqual([(plant.name, plant.level) for plant in PLANTS.values()], [
            ('馬鈴薯', 1), ('晨露藥草', 5), ('小麥', 10), ('魔女番茄', 20),
            ('月鈴草', 25), ('火紅辣椒', 30)])
        with self.assertRaises(CharacterError):
            self.farming.plant(1, 1, 'courtyard', 'dew_herb', now=0)
        with self.assertRaisesRegex(CharacterError, '農耕 Lv.20'):
            self.farming.plant(1, 1, 'prison', 'potato', now=0)
        self.set_level(20)
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        self.farming.plant(1, 1, 'prison', 'potato', now=10)
        with self.assertRaises(CharacterError):
            self.farming.plant(1, 1, 'courtyard', 'potato', now=20)
        self.assertEqual(set(self.farming.state(1, 1)['sessions']), {'courtyard', 'prison'})

    def test_harvest_is_atomic_exactly_once_and_survives_restart(self):
        self.farming.plant(1, 1, 'courtyard', 'potato', now=100)
        with self.assertRaises(CharacterError):
            self.farming.harvest(1, 1, 'courtyard', now=3699)
        other = RPGStore(self.path)
        try:
            farming = Farming(other, FixedRandom(0.99))
            result = farming.harvest(1, 1, 'courtyard', now=3700)
            self.assertEqual((result['quantity'], result['xp']), (2, 200))
            replay = farming.harvest(1, 1, 'courtyard', now=3701)
            self.assertTrue(replay['replayed'])
        finally:
            other.close()
        self.assertEqual(self.characters.inventory_counts(1, 1)['farming:potato'], 2)
        self.assertEqual(self.farming.state(1, 1)['xp'], 200)

    def test_level_snapshot_yield_bonus_and_cap(self):
        self.set_level(16)
        self.farming.rng = FixedRandom(0.49)  # 15-level gap: +1 guaranteed, 50% +1.
        started = self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        self.assertEqual(started['level_snapshot'], 16)
        self.set_level(50)  # Later level changes do not alter this crop's yield.
        result = self.farming.harvest(1, 1, 'courtyard', now=3600)
        self.assertEqual((result['base_yield'], result['level_bonus'], result['quantity']), (2, 2, 4))
        self.farming.plant(1, 1, 'courtyard', 'potato', now=4000)
        result = self.farming.harvest(1, 1, 'courtyard', now=7600)
        self.assertEqual((result['level_bonus'], result['quantity']), (3, 5))

    def test_cancel_selected_plot_discards_all_progress_and_rewards(self):
        self.set_level(20)
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        self.farming.plant(1, 1, 'prison', 'potato', now=10)
        result = self.farming.cancel(1, 1, 'courtyard')
        self.assertEqual(result, {'location_id': 'courtyard', 'plant_id': 'potato'})
        state = self.farming.state(1, 1)
        self.assertEqual(state['sessions']['courtyard']['status'], 'cancelled')
        self.assertEqual(state['sessions']['prison']['status'], 'active')
        self.assertEqual(state['xp'], level_floor(20))
        self.assertNotIn('farming:potato', self.characters.inventory_counts(1, 1))
        with self.assertRaisesRegex(CharacterError, '已經中斷'):
            self.farming.harvest(1, 1, 'courtyard', now=999999)
        with self.assertRaisesRegex(CharacterError, '沒有進行中'):
            self.farming.cancel(1, 1, 'courtyard')
        self.farming.plant(1, 1, 'courtyard', 'potato', now=20)
        self.assertEqual(self.farming.state(1, 1)['sessions']['courtyard']['status'], 'active')

    def test_maturity_notifications_are_per_plot_and_exactly_once(self):
        self.set_level(20)
        self.assertFalse(self.farming.state(1, 1)['notify'])
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        self.farming.plant(1, 1, 'prison', 'potato', now=10)
        self.assertEqual(self.farming.notifications_due(now=999999), [])
        self.assertTrue(self.farming.set_notify(1, 1, True))
        self.assertEqual(self.farming.notifications_due(now=3600),
                         [(1, 1, 'courtyard', 'potato', 0.0)])
        self.assertTrue(self.farming.reserve_notification(1, 1, 'courtyard', now=3600))
        self.assertFalse(self.farming.reserve_notification(1, 1, 'courtyard', now=3600))
        self.assertEqual(self.farming.notified_active(),
                         [(1, 1, 'courtyard', 'potato', 0.0)])
        self.assertEqual(self.farming.notifications_due(now=3610),
                         [(1, 1, 'prison', 'potato', 10.0)])

    def test_notification_harvest_rejects_harvested_or_replaced_crop(self):
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        self.farming.harvest(1, 1, 'courtyard', now=3600)
        with self.assertRaisesRegex(CharacterError, '不能重複領取'):
            self.farming.harvest(1, 1, 'courtyard', now=3601, expected_planted_at=0)
        self.farming.plant(1, 1, 'courtyard', 'potato', now=3602)
        with self.assertRaisesRegex(CharacterError, '已經收成'):
            self.farming.harvest(1, 1, 'courtyard', now=999999, expected_planted_at=0)


if __name__ == '__main__':
    unittest.main()
