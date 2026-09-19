import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from core.rpg_witch_rest import equipment_id
from core.rpg_witch_rest_workshop import EquipmentSelect, affix_roll_text, affix_text


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


class WitchRestWorkshopInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_equipment_select_keeps_parent_view_while_rebuilding(self):
        class RebuildingView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)
                self.selected = 1

            def rebuild(self):
                self.clear_items()

            def embed(self):
                return discord.Embed(title=f'裝備 #{self.selected}')

        view = RebuildingView()
        equipment = [
            {'instance_id': 1, 'item_id': equipment_id('ema', '騎士', 'weapon')},
            {'instance_id': 2, 'item_id': equipment_id('ema', '騎士', 'suit')},
        ]
        select = EquipmentSelect(view, equipment)
        view.add_item(select)
        select._values = ['2']
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

        await select.callback(interaction)

        self.assertEqual(view.selected, 2)
        interaction.response.edit_message.assert_awaited_once()
        self.assertIs(interaction.response.edit_message.call_args.kwargs['view'], view)
        self.assertEqual(
            interaction.response.edit_message.call_args.kwargs['embed'].title,
            '裝備 #2')


if __name__ == '__main__':
    unittest.main()
