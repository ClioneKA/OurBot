import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from core.rpg_witch_rest import equipment_id
from core.rpg_witch_rest_workshop import (
    DirectAffixConfirmView, DirectAffixPickerView, DirectAffixSelect, EquipmentSelect,
    affix_roll_text, affix_text,
)


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
            {'instance_id': 1, 'item_id': equipment_id('ema', '騎士', 'weapon'),
             'affixes': [(0, 'witch:assault:3', 10), (1, 'witch:haste:2', 2)]},
            {'instance_id': 2, 'item_id': equipment_id('ema', '騎士', 'suit'),
             'affixes': [(0, 'witch:vitality:2', 50), (1, 'witch:guard:4', 4)]},
        ]
        select = EquipmentSelect(view, equipment)
        self.assertEqual(select.options[0].description, '前綴：攻勢 III｜後綴：迅捷 II')
        self.assertEqual(select.options[1].description, '前綴：生命 II｜後綴：堅守 IV')
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

    async def test_direct_affix_uses_legal_choices_then_confirmation(self):
        item = {
            'instance_id': 1, 'item_id': equipment_id('ema', '騎士', 'weapon'),
            'witch_id': 'ema',
            'affixes': [(0, 'witch:assault:3', 10), (1, 'witch:haste:2', 2)],
        }
        rest = SimpleNamespace(
            _affix_value=lambda _item_id, _job, _index, kind, grade: {
                'precision': 4, 'critical': 2, 'prowess': 4,
                'stability': 4, 'drain': 2,
            }[kind],
            direct_affix=Mock(),
        )
        panel = SimpleNamespace(
            item=lambda: item,
            cog=SimpleNamespace(witch_rest=rest),
            owner=SimpleNamespace(id=10), guild_id=1, selected=1,
            embed=lambda notice=None: discord.Embed(title='魔女裝備', description=notice),
        )
        picker = DirectAffixPickerView(panel, 1)
        select = next(child for child in picker.children if isinstance(child, DirectAffixSelect))

        self.assertNotIn('haste', [option.value for option in select.options])
        critical = next(option for option in select.options if option.value == 'critical')
        self.assertEqual(critical.label, '會心 II')
        self.assertEqual(critical.description, '暴擊率 +2 個百分點')

        select._values = ['critical']
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        await select.callback(interaction)

        rest.direct_affix.assert_not_called()
        confirm = interaction.response.edit_message.call_args.kwargs['view']
        self.assertIsInstance(confirm, DirectAffixConfirmView)
        preview = interaction.response.edit_message.call_args.kwargs['embed'].description
        self.assertIn('迅捷 II（速度 +2）', preview)
        self.assertIn('會心 II（暴擊率 +2 個百分點）', preview)
        self.assertIn('確認後將消耗', preview)


if __name__ == '__main__':
    unittest.main()
