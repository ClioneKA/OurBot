from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_character import CharacterError, Characters, ITEMS, item_sell_price
from core.rpg_fishing import DURATIONS, Fishing, SPOTS, _weighted_pick, fishing_mastery
from core.settings import RPGSettings


class SequenceRandom:
    def __init__(self, values):
        self.values = iter(values)

    def random(self):
        return next(self.values)


class FishingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.fishing = Fishing(self.store)

    def grant(self, *keys):
        with self.store.db:
            for key in keys:
                self.store.db.execute('''INSERT INTO rpg_inventory(guild_id,user_id,item_id,quantity)
                    VALUES (1,1,?,1) ON CONFLICT(guild_id,user_id,item_id)
                    DO UPDATE SET quantity=quantity+1''', (key,))

    def test_rules_and_first_use_grants_one_bound_old_rod(self):
        self.assertEqual([(seconds, catches) for _, seconds, catches in DURATIONS.values()],
                         [(1800, 2), (7200, 6), (28800, 20)])
        self.assertEqual([spot.level for spot in SPOTS.values()], [1, 20])
        self.assertEqual([fishing_mastery(level, SPOTS['pond']) for level in (1, 11, 20, 31, 120)],
                         [0, 10, 10, 30, 30])
        self.assertTrue(all(sum(weight for _, weight in spot.loot) == 100 for spot in SPOTS.values()))
        state = self.fishing.state(1, 1)
        self.assertEqual((state['level'], state['rod_id']), (1, 'fishing:rod:old'))
        self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:rod:old'], 1)
        self.fishing.state(1, 1)
        self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:rod:old'], 1)
        with self.assertRaises(CharacterError):
            self.characters.dispose(1, 1, 'fishing:rod:old', 1, 2)
        with self.assertRaises(CharacterError):
            self.characters.dispose(1, 1, 'fishing:rod:old', 1)

    def test_level_gate_dispatch_snapshot_and_exactly_once_claim(self):
        with self.assertRaises(CharacterError):
            self.fishing.start(1, 1, 'lake', 'short', now=100)
        self.fishing.start(1, 1, 'pond', 'short', now=100)
        with self.assertRaises(CharacterError):
            self.fishing.start(1, 1, 'pond', 'short', now=101)
        with self.assertRaises(CharacterError):
            self.fishing.claim(1, 1, now=1899)
        self.fishing.rng = SequenceRandom([0.9, 0.0, 0.0])
        result = self.fishing.claim(1, 1, now=1900)
        self.assertEqual((result['catches'], result['xp']), (2, 200))
        self.assertEqual(result['items'], {'fishing:pond:common': 2})
        replay = self.fishing.claim(1, 1, now=1901)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.fishing.state(1, 1)['xp'], 200)
        self.assertEqual(self.characters.inventory_counts(1, 1)['fishing:pond:common'], 2)

    def test_rare_xp_rod_bonus_and_magic_weight(self):
        self.grant('fishing:pond:rod', 'fishing:pond:line', 'fishing:pond:hook')
        rod = self.fishing.craft_next(1, 1)
        self.assertEqual(rod.name, '簡易釣竿')
        self.assertEqual(self.fishing.state(1, 1)['rod_id'], 'fishing:rod:simple')
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        # Bonus succeeds; rolls 54.1% and 55% land in the rare-fish interval.
        self.fishing.rng = SequenceRandom([0.1, 0.541, 0.55, 0.0])
        result = self.fishing.claim(1, 1, now=1800)
        self.assertTrue(result['bonus'])
        self.assertEqual(result['catches'], 3)
        self.assertEqual(result['items']['fishing:pond:rare'], 2)
        self.assertEqual(result['xp'], 400)
        picked = _weighted_pick(SPOTS['lake'].loot, SPOTS['lake'].rare_item, 1.1,
                                SequenceRandom([0.54]))
        self.assertEqual(picked, 'fishing:lake:rare')

    def test_level_twenty_lake_xp_crafting_and_restart(self):
        self.fishing.state(1, 1)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_fishing_players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(20),))
        started = self.fishing.start(1, 1, 'lake', 'medium', now=10)
        self.assertEqual((started['base_catches'], started['rod_id']), (6, 'fishing:rod:old'))
        other = RPGStore(self.path)
        try:
            reloaded = Fishing(other, SequenceRandom([0.9] + [0.0] * 6))
            self.assertEqual(reloaded.state(1, 1)['session']['ready_at'], 7210)
            result = reloaded.claim(1, 1, now=7210)
            self.assertEqual((result['catches'], result['xp']), (6, 1800))
        finally:
            other.close()

    def test_level_mastery_adds_items_without_xp_and_uses_dispatch_snapshot(self):
        self.fishing.state(1, 1)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_fishing_players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(31),))
        started = self.fishing.start(1, 1, 'pond', 'short', now=0)
        self.assertEqual((started['level_snapshot'], started['mastery_percent']), (31, 30))
        with self.store.db:
            self.store.db.execute('UPDATE rpg_fishing_players SET xp=? WHERE guild_id=1 AND user_id=1',
                                  (level_floor(1),))
        # Rod roll; first common catch + successful mastery; second common catch + failed mastery.
        self.fishing.rng = SequenceRandom([0.9, 0.0, 0.1, 0.0, 0.4])
        result = self.fishing.claim(1, 1, now=1800)
        self.assertEqual((result['catches'], result['mastery_percent'], result['mastery_bonus']), (2, 30, 1))
        self.assertEqual(result['items'], {'fishing:pond:common': 3})
        self.assertEqual(result['xp'], 200)

    def test_crafting_is_atomic_and_uses_one_of_each_part(self):
        self.fishing.state(1, 1)
        self.grant('fishing:pond:rod', 'fishing:pond:line')
        before = self.characters.inventory_counts(1, 1)
        with self.assertRaises(CharacterError):
            self.fishing.craft_next(1, 1)
        self.assertEqual(self.characters.inventory_counts(1, 1), before)
        self.grant('fishing:pond:hook')
        self.fishing.craft_next(1, 1)
        counts = self.characters.inventory_counts(1, 1)
        self.assertNotIn('fishing:rod:old', counts)
        self.assertEqual(counts['fishing:rod:simple'], 1)
        self.grant('fishing:lake:rod', 'fishing:lake:line', 'fishing:lake:hook')
        self.fishing.craft_next(1, 1)
        counts = self.characters.inventory_counts(1, 1)
        self.assertNotIn('fishing:rod:simple', counts)
        self.assertEqual(counts['fishing:rod:magic'], 1)

    def test_exact_sale_prices_transfer_and_notifications(self):
        self.grant('fishing:pond:coin', 'fishing:pond:common')
        self.assertEqual(item_sell_price(ITEMS['fishing:pond:coin']), 100)
        self.assertEqual(self.characters.dispose(1, 1, 'fishing:pond:coin', 1), 100)
        self.characters.dispose(1, 1, 'fishing:pond:common', 1, 2)
        self.assertEqual(self.characters.inventory_counts(1, 2)['fishing:pond:common'], 1)
        self.assertEqual(self.fishing.notifications_due(now=999999), [])
        self.fishing.set_notify(1, 1, True)
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        self.assertEqual(self.fishing.notifications_due(now=1800), [(1, 1, 'pond', 'short', 0.0)])
        self.assertTrue(self.fishing.reserve_notification(1, 1, now=1800))
        self.assertFalse(self.fishing.reserve_notification(1, 1, now=1800))
        self.assertEqual(self.fishing.notified_active(), [(1, 1, 'pond', 'short', 0.0)])

    def test_notification_claim_rejects_claimed_or_replaced_session(self):
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        self.fishing.rng = SequenceRandom([0.9, 0.0, 0.0])
        self.fishing.claim(1, 1, now=1800)
        with self.assertRaisesRegex(CharacterError, '不能重複領取'):
            self.fishing.claim(1, 1, now=1801, expected_started_at=0)
        self.fishing.start(1, 1, 'pond', 'short', now=1802)
        with self.assertRaisesRegex(CharacterError, '已經結束'):
            self.fishing.claim(1, 1, now=999999, expected_started_at=0)


if __name__ == '__main__':
    unittest.main()
