import math
import unittest

from core.rpg_witch_scaling import level_scales, witch_stat_scales


class WitchScalingTests(unittest.TestCase):
    def test_four_player_level50_keeps_the_validated_reference(self):
        scales = witch_stat_scales(50, 4)
        self.assertAlmostEqual(scales['HP'], .7)
        self.assertAlmostEqual(scales['攻擊'], .8419189453125)
        self.assertAlmostEqual(scales['防禦'], 1)

    def test_party_size_compensates_only_hp_and_partial_attack(self):
        reference = witch_stat_scales(50, 4)
        for size in range(1, 7):
            scaled = witch_stat_scales(50, size)
            self.assertAlmostEqual(scaled['HP'] / reference['HP'], size / 4)
            self.assertAlmostEqual(scaled['攻擊'] / reference['攻擊'], math.sqrt(size / 4))
            self.assertEqual(scaled['防禦'], reference['防禦'])

    def test_fractional_average_interpolates_without_rounding_players(self):
        low, high = level_scales(45), level_scales(49)
        self.assertEqual(level_scales(47), tuple(a + (b - a) / 2 for a, b in zip(low, high)))

    def test_unlock_boundaries_do_not_apply_the_next_tier_early(self):
        self.assertLess(level_scales(19)[1], .20)
        self.assertGreater(level_scales(20)[1], .30)
        self.assertLess(level_scales(49)[1], .67)
        self.assertGreater(level_scales(50)[1], .84)

    def test_level_curve_is_positive_monotonic_and_bounded(self):
        previous = (0, 0, 0)
        for half_level in range(2, 241):
            current = level_scales(half_level / 2)
            self.assertTrue(all(value > 0 for value in current))
            self.assertTrue(all(value >= old for value, old in zip(current, previous)))
            previous = current
        self.assertEqual(level_scales(0), level_scales(1))
        self.assertEqual(level_scales(200), level_scales(120))
        with self.assertRaises(ValueError):
            witch_stat_scales(50, 0)
