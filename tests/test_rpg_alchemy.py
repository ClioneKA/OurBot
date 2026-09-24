from pathlib import Path
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.rpg import RPGStore, level_floor
from core.rpg_alchemy import (AlchemyDolls, BODY_BUDGETS, CORE_ITEM, FUEL_CAPACITY, POWDER_ITEM,
                              RARITY_ORDER, body_acceleration_cost, fuel_value,
                              fuel_discount, operation_fuel_cost,
                              life_skill_unlocks, life_xp_percent, material_profile, parse_stone,
                              raid_signup_pool, stat_caps, stone_id)
from core.rpg_character import Characters, CharacterError, ITEMS
from core.rpg_farming import Farming
from core.rpg_fishing import Fishing
from core.rpg_provisions import Provisions
from core.rpg_battle import raid_battle
from core.rpg_alchemy_view import AlchemyView, combat_stone_effect
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


class AlchemyAutomationPoolTests(unittest.TestCase):
    def test_paint_set_raid_uses_mid_tier_filters(self):
        self.assertEqual(raid_signup_pool({'pool': 'special'}), 'mid')
        self.assertEqual(raid_signup_pool({'pool': 'regular'}), 'regular')

    def test_life_xp_study_thresholds(self):
        self.assertEqual([life_xp_percent(work) for work in
                          (0, 19, 20, 49, 50, 89, 90, 139, 140, 199, 200, 999)],
                         [0, 0, 5, 5, 8, 8, 12, 12, 16, 16, 20, 20])


class AlchemyDollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.characters.create(1, 10)
        self.alchemy = AlchemyDolls(self.store, RPGSettings(), FixedRng())

    def test_auto_cooking_skips_host_with_open_public_table(self):
        self.alchemy.configure_life(
            1, 10, 'cooking', enabled=True, preset_slot=1,
            pools=('regular',), qualities=('普通',), require_no_effect=False)
        open_table = [False]
        provisions = SimpleNamespace(
            preset=lambda *_: {'ingredients': ['farming:potato'] * 5},
            preview=lambda *_: {'score': 1},
            _active_meal=lambda *_: None,
            _host_has_open_table=lambda *_: open_table[0],
        )
        raid = {'id': 'r1', 'guild_id': 1, 'source': None, 'pool': 'regular',
                'monster': {'quality': '普通'}}
        with patch.object(self.alchemy, 'life_skill', return_value={'work': 100}):
            self.assertEqual(self.alchemy.auto_cooking_candidates(raid, provisions),
                             [(10, ['farming:potato'] * 5)])
            raid['source'] = 'divination'
            self.assertEqual(self.alchemy.auto_cooking_candidates(raid, provisions),
                             [(10, ['farming:potato'] * 5)])
            open_table[0] = True
            self.assertEqual(self.alchemy.auto_cooking_candidates(raid, provisions), [])

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

    def test_body_acceleration_charges_for_exact_remaining_time(self):
        self.characters.grant_item(1, 10, 'farming:wheat', 10)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,10,1000)')
        craft = self.alchemy.start_body(1, 10, ['farming:wheat'] * 10, now=100)
        self.assertEqual(body_acceleration_cost(craft, now=100), 250)
        self.assertEqual(self.alchemy.accelerate_body(1, 10, now=100), 250)
        self.assertEqual(self.store.gold(1, 10), 750)
        body = self.alchemy.finish_body(1, 10, now=100)
        self.assertEqual(body['tier'], 10)

    def test_core_page_builds_without_a_core_and_triggers_explain_missing_doll(self):
        self.make_body()
        self.characters.grant_item(1, 10, 'farming:wheat', 10)
        craft = self.alchemy.start_body(1, 10, ['farming:wheat'] * 10, now=100)
        self.alchemy.finish_body(1, 10, now=craft['ready_at'])

        async def check():
            provisions = SimpleNamespace(presets=lambda guild, user: [])
            cog = SimpleNamespace(alchemy=self.alchemy, characters=self.characters,
                                  provisions=provisions, store=self.store)
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=10, mention='<@10>'), guild_id=1,
                response=SimpleNamespace(send_message=AsyncMock()))
            view = AlchemyView(cog, interaction)
            view.page = 'cores'
            self.store.set_menu_favorites(1, 10, ('alchemy:cores',))
            view.rebuild()
            self.assertGreater(len(view.children), 0)
            self.assertTrue(any(getattr(child, 'label', '') == '★ 移除最愛'
                                for child in view.children))
            view.page = 'body'
            view.rebuild()
            self.assertTrue(any(getattr(child, 'label', '') == '☆ 加入最愛'
                                for child in view.children))
            self.assertEqual(self.store.menu_favorites(1, 10), ('alchemy:cores',))
            view.page = 'cores'
            view.rebuild()
            await view.handle(interaction, 'triggers')
            interaction.response.send_message.assert_awaited_once()
            self.characters.grant_item(1, 10, CORE_ITEM[1])
            self.characters.grant_item(1, 10, stone_id('life', 'fishing', '普通'), 2)
            self.characters.grant_item(1, 10, 'farming:wheat', 2)
            view.core_id = self.alchemy.orient_core(1, 10, 1, '生活')
            view.rebuild()
            self.assertGreater(len(view.children), 0)
            for page in ('overview', 'automation', 'stats', 'decompose', 'fuel'):
                view.page = page
                view.rebuild()
                self.assertGreater(len(view.children), 0)
            view.page = 'body'
            body_embed = view.embed()
            self.assertEqual([field.name for field in body_embed.fields],
                             ['當前素體 T10', '候選素體 T10'])

        asyncio.run(check())

    def test_core_orientation_engraving_and_support_snapshot(self):
        body = self.make_body()
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '戰鬥')
        skill_item = stone_id('combat', 'power_strike', '傳說')
        self.characters.grant_item(1, 10, skill_item, 2)
        skills = self.alchemy.engrave(1, 10, core_id, 'combat', 1, skill_item)
        self.assertEqual(skills['combat'][0], {'key': 'power_strike', 'rarity': '傳說'})
        with self.assertRaisesRegex(CharacterError, '完全相同'):
            self.alchemy.engrave(1, 10, core_id, 'combat', 1, skill_item)
        self.assertEqual(self.inventory()[skill_item], 1)
        with self.assertRaisesRegex(CharacterError, '不能重複'):
            self.alchemy.engrave(1, 10, core_id, 'combat', 2, skill_item)
        self.assertIsNone(self.alchemy.support(1, 10))
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=1000 '
                                  'WHERE guild_id=1 AND user_id=10')
        self.alchemy.set_combat_support(1, 10, True)
        expected_cost = operation_fuel_cost(body)
        support = self.alchemy.prepare_support(1, 10, 'raid:123', now=200)
        self.assertEqual(support['stats']['HP'], 50 + body['stats'][0] * 10)
        self.assertEqual(len(support['skills']), 1)
        self.assertEqual((support['skills'][0]['maximum'], support['skills'][0]['remaining']),
                         (3, 3))
        self.assertEqual(support['fuel_cost'], expected_cost)
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 1000 - expected_cost)
        self.assertEqual(self.alchemy.prepare_support(1, 10, 'raid:123', now=201), support)
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 1000 - expected_cost)

        human = dict(id=99, name='測試者', state=self.characters.snapshot(1, 10),
                     rules=[], basic_target='lowest', doll_support=support)
        monster = prepare_monster({'kind': '巨獸', 'name': '測試怪物'}, '普通')
        with_support = raid_battle([human], monster, 1)
        without_support = raid_battle([{**human, 'doll_support': None}], monster, 1)
        self.assertEqual(with_support.fighters[-1].stats['HP'],
                         without_support.fighters[-1].stats['HP'])

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
        self.assertNotEqual(self.alchemy._draw_rarity(49, 0), '普通')
        self.assertEqual(self.alchemy._draw_rarity(0, 99), '傳說')
        with self.store.db:
            self.store.db.execute('''INSERT INTO rpg_alchemy_pity VALUES (1,10,42,87)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET epic_misses=42,legend_misses=87''')
        self.assertEqual(self.alchemy.pity(1, 10)['epic_remaining'], 8)
        self.assertEqual(self.alchemy.pity(1, 10)['legend_remaining'], 13)

    def test_main_panel_shows_calculated_stone_effects_and_gacha_gold(self):
        body = self.make_body()
        body['stats'] = [70, 70, 70, 70, 70]
        candidate = dict(body, stats=[100, 100, 100, 100, 100])
        with self.store.db:
            self.store.db.execute('''UPDATE rpg_alchemy_dolls
                SET active_body=?,candidate_body=? WHERE guild_id=1 AND user_id=10''',
                (__import__('json').dumps(body), __import__('json').dumps(candidate)))
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '生活')
        combat_item = stone_id('combat', 'power_strike', '稀有')
        life_item = stone_id('life', 'fishing', '史詩')
        self.characters.grant_item(1, 10, combat_item, 2)
        self.characters.grant_item(1, 10, life_item)
        self.alchemy.engrave(1, 10, core_id, 'combat', 1, combat_item)
        self.alchemy.engrave(1, 10, core_id, 'life', 1, life_item)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,10,4321)')
            self.store.db.execute('INSERT INTO rpg_alchemy_pity VALUES (1,10,42,87)')

        provisions = SimpleNamespace(presets=lambda guild, user: [])
        cog = SimpleNamespace(alchemy=self.alchemy, characters=self.characters,
                              provisions=provisions, store=self.store)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10, mention='<@10>'), guild_id=1)
        view = AlchemyView(cog, interaction)
        overview = view.embed()
        fields = {field.name: field.value for field in overview.fields}
        self.assertIn('稀有・動力重擊｜160% 攻擊',
                      fields['戰鬥技能石｜稀有度計算後'])
        expected_work = self.alchemy.life_skill(1, 10, 'fishing')['work']
        self.assertIn(f'史詩・自律釣魚｜工作力 {expected_work:,}',
                      fields['生活技能石｜稀有度計算後'])
        self.assertIn('已解鎖：30 分鐘', fields['生活技能石｜稀有度計算後'])

        view.page = 'gacha'
        self.assertIn('**目前金幣**\u30004,321', view.embed().description)
        self.assertEqual(combat_stone_effect('cleanse', '傳說', body),
                         '移除 3 個負面狀態')
        self.assertEqual(life_skill_unlocks('farming', 93), ('中庭花圃', '監獄菜園'))
        self.assertEqual(life_skill_unlocks('farming', 94),
                         ('中庭花圃', '監獄菜園', '廢棄溫室'))

        view.page = 'cores'
        view.core_id = core_id
        view.slot = 'combat:1'
        view.item_id = combat_item
        view.rebuild()
        slot_select = next(child for child in view.children
                           if getattr(child, 'action', None) == 'slot')
        self.assertIn('稀有・動力重擊', slot_select.options[0].label)
        engrave_button = next(child for child in view.children
                              if getattr(child, 'label', None) == '刻入技能石')
        self.assertTrue(engrave_button.disabled)
        core_fields = {field.name: field.value for field in view.embed().fields}
        self.assertIn('160% 攻擊', core_fields[f'核心 #{core_id}｜戰鬥迴路'])
        self.assertIn('稀有・動力重擊', core_fields['待刻印技能石'])

        legendary_life = stone_id('life', 'fishing', '傳說')
        self.characters.grant_item(1, 10, legendary_life)
        view.slot = 'life:1'
        view.item_id = legendary_life
        view.rebuild()
        preview_fields = {field.name: field.value for field in view.embed().fields}
        skill_preview = preview_fields['刻印後｜生活工作力預覽']
        self.assertIn('目前　史詩・自律釣魚｜工作力 63', skill_preview)
        self.assertIn('刻印後　傳說・自律釣魚｜工作力 70', skill_preview)
        self.assertIn('2 小時', skill_preview)

        view.page = 'body'
        body_fields = {field.name: field.value for field in view.embed().fields}
        body_preview = body_fields['安裝候選素體｜生活工作力預覽']
        self.assertIn('目前　工作力 63', body_preview)
        self.assertIn('安裝後　工作力 90', body_preview)
        self.assertIn('2 小時', body_preview)

        view.page = 'gacha'
        gacha = view.embed().description
        self.assertIn('史詩以上保底剩 **8** 抽', gacha)
        self.assertIn('傳說保底剩 **13** 抽', gacha)

    def test_study_stones_increase_each_matching_life_skill_xp(self):
        body = self.make_body()
        body['stats'] = [100, 100, 100, 100, 100]
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=? '
                                  'WHERE guild_id=1 AND user_id=10',
                                  (__import__('json').dumps(body),))
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '生活')
        for slot, key in enumerate(('fishing_study', 'farming_study', 'cooking_study'), 1):
            item_id = stone_id('life', key, '普通')
            self.characters.grant_item(1, 10, item_id)
            self.alchemy.engrave(1, 10, core_id, 'life', slot, item_id)
        for activity in ('fishing', 'farming', 'cooking'):
            self.assertEqual(self.alchemy.life_xp_bonus_percent(1, 10, activity), 8)
        self.assertEqual(life_skill_unlocks('fishing_study', 70), ('XP +8%',))

        fishing = Fishing(self.store, rng=FixedRng(.99), boss_rng=FixedRng(.99),
                          material_rng=FixedRng(.99),
                          xp_bonus=self.alchemy.life_xp_bonus_percent)
        trip = fishing.start(1, 10, 'pond', 'short', now=0)
        fish_result = fishing.claim(1, 10, now=trip['ready_at'])
        self.assertEqual((fish_result['study_bonus_xp'], fish_result['xp']), (10, 140))

        farming = Farming(self.store, rng=FixedRng(.99), material_rng=FixedRng(.99),
                          specialization_rng=FixedRng(.99),
                          xp_bonus=self.alchemy.life_xp_bonus_percent)
        crop = farming.plant(1, 10, 'courtyard', 'potato', now=0)
        farm_result = farming.harvest(1, 10, 'courtyard', now=crop['ready_at'])
        self.assertEqual((farm_result['study_bonus_xp'], farm_result['xp']), (16, 216))

        provisions = Provisions(self.store, xp_bonus=self.alchemy.life_xp_bonus_percent)
        self.characters.grant_item(1, 10, 'fishing:pond:common', 5)
        cooking_result = provisions.donate(1, 10, ['fishing:pond:common'] * 5)
        self.assertEqual((cooking_result['study_bonus_xp'], cooking_result['xp']), (10, 135))

        self.characters.grant_item(1, 10, 'fishing:pond:common', 5)
        meal = provisions.cook(1, 10, 9, ['fishing:pond:common'] * 5, now=100)
        self.assertEqual((meal['data']['base_cooking_xp'], meal['data']['study_bonus_xp'],
                          meal['data']['cooking_xp'], meal['data']['initial_cooking_xp']),
                         (200, 16, 216, 54))
        provisions.publish(meal['id'], 99)
        provisions.claim(meal['id'], 1, 11, now=101)
        provisions.claim(meal['id'], 1, 12, now=102)
        self.assertEqual(provisions.state(1, 10)['xp'], 135 + 216)

    def test_auto_farming_checks_each_plots_work_unlock(self):
        self.make_body()
        body = self.alchemy.state(1, 10)['active_body']
        body['stats'] = [30, 30, 30, 30, 30]
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=?,fuel=500 '
                                  'WHERE guild_id=1 AND user_id=10',
                                  (__import__('json').dumps(body),))
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '生活')
        item_id = stone_id('life', 'farming', '普通')
        self.characters.grant_item(1, 10, item_id)
        self.alchemy.engrave(1, 10, core_id, 'life', 1, item_id)
        self.alchemy.configure_life(1, 10, 'farming', enabled=True)

        farming = Farming(self.store, rng=FixedRng(.99), material_rng=FixedRng(.99),
                          specialization_rng=FixedRng(.99))
        farming.state(1, 10)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_farming_players SET xp=? '
                                  'WHERE guild_id=1 AND user_id=10', (level_floor(60),))
        courtyard = farming.plant(1, 10, 'courtyard', 'potato', now=0)
        prison = farming.plant(1, 10, 'prison', 'potato', now=0)

        first = self.alchemy.auto_farm(
            farming, 1, 10, 'courtyard', 'potato', 0, now=courtyard['ready_at'])
        locked = self.alchemy.auto_farm(
            farming, 1, 10, 'prison', 'potato', 0, now=prison['ready_at'])
        self.assertTrue(first['replanted'])
        self.assertIsNone(locked)
        self.assertEqual(farming.state(1, 10)['sessions']['prison']['status'], 'active')

        body['stats'] = [70, 70, 30, 30, 30]  # 普通石農耕工作力 49，解鎖監獄菜園。
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET active_body=? '
                                  'WHERE guild_id=1 AND user_id=10',
                                  (__import__('json').dumps(body),))
        unlocked = self.alchemy.auto_farm(
            farming, 1, 10, 'prison', 'potato', 0, now=prison['ready_at'])
        self.assertTrue(unlocked['replanted'])

    def test_batch_decompose_uses_all_selected_stone_quantities(self):
        common = stone_id('life', 'fishing', '普通')
        rare = stone_id('life', 'farming', '稀有')
        self.characters.grant_item(1, 10, common, 2)
        self.characters.grant_item(1, 10, rare, 3)
        quantity, powder = self.alchemy.decompose_many(1, 10, [common, rare, common])
        self.assertEqual((quantity, powder), (5, 11))
        self.assertEqual(self.inventory()[POWDER_ITEM], 11)
        self.assertNotIn(common, self.inventory())
        self.assertNotIn(rare, self.inventory())

    def test_dismantle_core_turns_engraved_stones_into_powder(self):
        self.make_body()
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '生活')
        common = stone_id('life', 'fishing', '普通')
        epic = stone_id('life', 'farming', '史詩')
        self.characters.grant_item(1, 10, common)
        self.characters.grant_item(1, 10, epic)
        self.alchemy.engrave(1, 10, core_id, 'life', 1, common)
        self.alchemy.engrave(1, 10, core_id, 'life', 2, epic)
        result = self.alchemy.dismantle_core(1, 10, core_id)

        self.assertEqual(result, {'stones': 2, 'powder': 11, 'equipped': True})
        self.assertEqual(self.alchemy.cores(1, 10), [])
        self.assertEqual(self.inventory()[POWDER_ITEM], 11)
        with self.assertRaisesRegex(CharacterError, '找不到'):
            self.alchemy.dismantle_core(1, 10, core_id)

    def test_fuel_conversion_uses_sell_value(self):
        self.characters.grant_item(1, 10, 'farming:wheat', 2)
        gained = self.alchemy.convert_fuel(1, 10, 'farming:wheat', 2)
        self.assertEqual(gained, fuel_value(ITEMS['farming:wheat']) * 2)
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], gained)
        self.assertNotIn('farming:wheat', self.inventory())
        per_item = fuel_value(ITEMS['farming:wheat'])
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=? '
                                  'WHERE guild_id=1 AND user_id=10', (1000 - per_item,))
        self.characters.grant_item(1, 10, 'farming:wheat', 2)
        self.assertEqual(self.alchemy.convert_fuel(1, 10, 'farming:wheat', 2), per_item)
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 1000)
        self.assertNotIn('farming:wheat', self.inventory())
        self.characters.grant_item(1, 10, 'farming:wheat')
        with self.assertRaisesRegex(CharacterError, '已達上限'):
            self.alchemy.convert_fuel(1, 10, 'farming:wheat', 1)

    def test_fuel_page_offers_near_capacity_and_all_quantities(self):
        item_id = 'farming:wheat'
        per_item = fuel_value(ITEMS[item_id])
        self.characters.grant_item(1, 10, item_id, 20)
        self.alchemy.state(1, 10)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=? '
                                  'WHERE guild_id=1 AND user_id=10',
                                  (FUEL_CAPACITY - per_item * 7 - 1,))

        provisions = SimpleNamespace(presets=lambda guild, user: [])
        cog = SimpleNamespace(alchemy=self.alchemy, characters=self.characters,
                              provisions=provisions, store=self.store)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10, mention='<@10>'), guild_id=1)
        view = AlchemyView(cog, interaction)
        view.page = 'fuel'
        view.fuel_item_id = item_id
        view.rebuild()
        select = next(child for child in view.children
                      if getattr(child, 'action', None) == 'fuel_quantity')
        labels = {option.value: option.label for option in select.options}

        self.assertEqual(labels['7'], '補至接近上限')
        self.assertEqual(labels['20'], '全部持有數量')

    def test_fuel_page_can_sort_by_material_tier_or_unit_fuel(self):
        owned = ('farming:wheat', 'farming:eclipse_pepper', 'fishing:pond:coin')
        for key in owned:
            self.characters.grant_item(1, 10, key)

        provisions = SimpleNamespace(presets=lambda guild, user: [])
        cog = SimpleNamespace(alchemy=self.alchemy, characters=self.characters,
                              provisions=provisions, store=self.store)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10, mention='<@10>'), guild_id=1)
        view = AlchemyView(cog, interaction)
        view.page = 'fuel'

        view.fuel_sort = 'tier_desc'
        view.rebuild()
        picker = next(child for child in view.children
                      if getattr(child, 'action', None) == 'fuel_item')
        self.assertEqual(picker.options[0].value, 'farming:eclipse_pepper')
        self.assertIn('素材 T110', picker.options[0].description)

        view.fuel_sort = 'fuel_desc'
        view.rebuild()
        picker = next(child for child in view.children
                      if getattr(child, 'action', None) == 'fuel_item')
        self.assertEqual(picker.options[0].value, 'farming:eclipse_pepper')
        view.fuel_sort = 'fuel_asc'
        view.rebuild()
        picker = next(child for child in view.children
                      if getattr(child, 'action', None) == 'fuel_item')
        self.assertEqual(picker.options[0].value, 'farming:wheat')
        sorter = next(child for child in view.children
                      if getattr(child, 'action', None) == 'fuel_sort')
        self.assertEqual(len(sorter.options), 4)

    def test_mag_affinity_increases_actual_fuel_capacity(self):
        self.alchemy.state(1, 10)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_mag_affinity VALUES (1,10,100)')
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=1000 '
                                  'WHERE guild_id=1 AND user_id=10')
        self.assertEqual(self.alchemy.state(1, 10)['fuel_capacity'], 2000)
        self.characters.grant_item(1, 10, 'farming:wheat')
        self.assertGreater(self.alchemy.convert_fuel(1, 10, 'farming:wheat', 1), 0)

    def test_durability_uses_smooth_fuel_discount_capped_at_seventy_percent(self):
        body = {'stats': [0, 0, 30, 0, 0]}
        self.assertEqual(fuel_discount(0), 0)
        self.assertEqual(fuel_discount(30), 23)
        self.assertEqual(operation_fuel_cost(body), 77)
        self.assertEqual(fuel_discount(234), 70)
        self.assertEqual(fuel_discount(10000), 70)

    def test_low_fuel_alert_is_queued_once_and_rearmed_after_refill(self):
        body = self.make_body()
        self.characters.grant_item(1, 10, CORE_ITEM[1])
        core_id = self.alchemy.orient_core(1, 10, 1, '戰鬥')
        stone = stone_id('combat', 'power_strike', '普通')
        self.characters.grant_item(1, 10, stone)
        self.alchemy.engrave(1, 10, core_id, 'combat', 1, stone)
        self.alchemy.set_combat_support(1, 10, True)
        cost = operation_fuel_cost(body)
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=? '
                                  'WHERE guild_id=1 AND user_id=10', (cost * 6,))

        self.assertIsNotNone(self.alchemy.prepare_support(1, 10, 'raid:first', now=1))
        self.assertEqual(self.alchemy.fuel_alerts_due(), [])
        self.assertIsNotNone(self.alchemy.prepare_support(1, 10, 'raid:second', now=2))
        self.assertEqual(self.alchemy.fuel_alerts_due(), [(1, 10, cost * 4, cost)])
        self.assertTrue(self.alchemy.reserve_fuel_alert(1, 10))
        self.assertFalse(self.alchemy.reserve_fuel_alert(1, 10))
        self.assertEqual(self.alchemy.fuel_alerts_due(), [])

        per_item = fuel_value(ITEMS['farming:wheat'])
        quantity = (cost + per_item - 1) // per_item
        self.characters.grant_item(1, 10, 'farming:wheat', quantity)
        self.alchemy.convert_fuel(1, 10, 'farming:wheat', quantity)
        self.assertIsNotNone(self.alchemy.prepare_support(1, 10, 'raid:third', now=3))
        remaining = cost * 3 + per_item * quantity
        self.assertEqual(self.alchemy.fuel_alerts_due(), [(1, 10, remaining, cost)])

    def test_life_automation_collection_and_restart_cost_one_operation(self):
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
            self.alchemy.configure_life(1, 10, key, enabled=True)
        signup_item = stone_id('life', 'raid_signup', '普通')
        self.characters.grant_item(1, 10, signup_item)
        self.alchemy.engrave(1, 10, core_id, 'life', 3, signup_item)
        self.alchemy.configure_life(1, 10, 'raid_signup', enabled=True,
                                    pools=('regular',), qualities=('普通',))
        raid = {'id': 'r1', 'guild_id': 1, 'source': None, 'pool': 'regular',
                'monster': {'quality': '普通'}}
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [10])
        raid['source'] = 'divination'
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [10])
        self.alchemy.configure_life(1, 10, 'raid_signup', scheduled=False)
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [])
        self.alchemy.configure_life(1, 10, 'raid_signup', scheduled=True)
        raid['source'] = None
        with self.store.db:
            for index in range(4):
                self.store.db.execute('''INSERT INTO rpg_alchemy_operations
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (1, 10, 'raid_signup', f'old-{index}', 'completed', 100, '{}', index))
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [10])
        raid['pool'] = 'mid'
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [])
        raid['pool'] = 'special'
        self.assertEqual(self.alchemy.auto_signup_candidates(raid), [])
        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=400 '
                                  'WHERE guild_id=1 AND user_id=10')

        fishing = Fishing(self.store, rng=FixedRng(.99), boss_rng=FixedRng(.99),
                          material_rng=FixedRng(.99))
        farming = Farming(self.store, rng=FixedRng(.99), material_rng=FixedRng(.99),
                          specialization_rng=FixedRng(.99))
        fish = fishing.start(1, 10, 'pond', 'short', now=0)
        crop = farming.plant(1, 10, 'courtyard', 'potato', now=0)
        fishing.set_notify(1, 10, True)
        farming.set_notify(1, 10, True)
        self.assertEqual(fishing.notifications_due(now=fish['ready_at']),
                         [(1, 10, 'pond', 'short', 0.0)])
        self.assertEqual(farming.notifications_due(now=crop['ready_at']),
                         [(1, 10, 'courtyard', 'potato', 0.0)])
        self.alchemy.configure_life(1, 10, 'fishing', enabled=False)
        self.alchemy.configure_life(1, 10, 'farming', enabled=False)
        self.assertEqual(self.alchemy.fishing_notifications_due(
            fishing, now=fish['ready_at']), fishing.notifications_due(now=fish['ready_at']))
        self.assertEqual(self.alchemy.farming_notifications_due(
            farming, now=crop['ready_at']), farming.notifications_due(now=crop['ready_at']))
        self.alchemy.configure_life(1, 10, 'fishing', enabled=True)
        self.alchemy.configure_life(1, 10, 'farming', enabled=True)
        self.assertEqual(self.alchemy.fishing_notifications_due(
            fishing, now=fish['ready_at']), [])
        self.assertEqual(self.alchemy.farming_notifications_due(
            farming, now=crop['ready_at']), [])
        fish_result = self.alchemy.auto_fish(
            fishing, 1, 10, 'pond', 'short', 0, now=fish['ready_at'])
        farm_result = self.alchemy.auto_farm(
            farming, 1, 10, 'courtyard', 'potato', 0, now=crop['ready_at'])
        self.assertTrue(fish_result['restarted'])
        self.assertTrue(farm_result['replanted'])
        self.assertEqual((fish_result['fuel'], farm_result['fuel']), (77, 77))
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 246)
        self.assertEqual(fishing.state(1, 10)['session']['status'], 'active')
        self.assertEqual(farming.state(1, 10)['sessions']['courtyard']['status'], 'active')

        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=77 '
                                  'WHERE guild_id=1 AND user_id=10')
        fish = fishing.state(1, 10)['session']
        fish_result = self.alchemy.auto_fish(
            fishing, 1, 10, 'pond', 'short', fish['started_at'], now=fish['ready_at'])
        self.assertTrue(fish_result['restarted'])
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 0)
        self.assertEqual(fishing.state(1, 10)['session']['status'], 'active')

        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=77 '
                                  'WHERE guild_id=1 AND user_id=10')
        crop = farming.state(1, 10)['sessions']['courtyard']
        farm_result = self.alchemy.auto_farm(
            farming, 1, 10, 'courtyard', 'potato', crop['planted_at'],
            now=crop['ready_at'])
        self.assertTrue(farm_result['replanted'])
        self.assertEqual(self.alchemy.state(1, 10)['fuel'], 0)
        self.assertEqual(farming.state(1, 10)['sessions']['courtyard']['status'], 'active')

        with self.store.db:
            self.store.db.execute('UPDATE rpg_alchemy_dolls SET fuel=76 '
                                  'WHERE guild_id=1 AND user_id=10')
        fish = fishing.state(1, 10)['session']
        crop = farming.state(1, 10)['sessions']['courtyard']
        self.assertEqual(self.alchemy.fishing_notifications_due(
            fishing, now=fish['ready_at']), fishing.notifications_due(now=fish['ready_at']))
        self.assertEqual(self.alchemy.farming_notifications_due(
            farming, now=crop['ready_at']), farming.notifications_due(now=crop['ready_at']))
        crop = farming.state(1, 10)['sessions']['courtyard']
        with self.assertRaisesRegex(CharacterError, '燃料不足'):
            self.alchemy.auto_farm(
                farming, 1, 10, 'courtyard', 'potato', crop['planted_at'],
                now=crop['ready_at'])
        self.assertEqual(farming.state(1, 10)['sessions']['courtyard']['status'], 'active')


if __name__ == '__main__':
    unittest.main()
