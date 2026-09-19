import unittest

from core.rpg_witch_rest_workshop import affix_roll_text, affix_text


class WitchRestWorkshopTextTests(unittest.TestCase):
    def test_prefix_names_the_affected_stat(self):
        self.assertEqual(
            affix_text((0, 'witch:vitality:2', 100)),
            '生命 II（HP +100）')

    def test_weapon_suffix_explains_percentage_effect(self):
        self.assertEqual(
            affix_roll_text('stability', 4, 8),
            '穩定 IV（武器穩定度下限 +8 個百分點）')

    def test_suit_suffix_explains_condition_and_reduction(self):
        self.assertEqual(
            affix_roll_text('unyielding', 3, 7),
            '不屈 III（HP 低於 35% 時受到傷害 -7%）')


if __name__ == '__main__':
    unittest.main()
