from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from weakref import WeakSet
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord
from core.rpg import RPGStore
from core.rpg_character import Characters, JOBS
from core.rpg_divination import Divinations
from core.rpg_divination_view import DivinationView
from core.rpg_item_use_view import ItemUseView
from core.rpg_menu import AdventureView, FAVORITE_PAGES
from core.rpg_help import HELP_TOPICS
from core.rpg_painted_maze_rewards import PaintedMazeRewardStore
from core.rpg_profile_view import ProfileCardView
from core.settings import RPGSettings


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        settings = RPGSettings()
        self.characters = Characters(self.store, settings)
        self.divinations = Divinations(self.store)
        self.rewards = PaintedMazeRewardStore(self.store)
        self.cog = SimpleNamespace(store=self.store, characters=self.characters, settings=settings, menu_views=WeakSet(),
                                   divinations=self.divinations,
                                   painted_maze=SimpleNamespace(
                                       create=AsyncMock(return_value={'number': 7}),
                                       rewards=self.rewards),
                                   character_embed=lambda *args: discord.Embed(title='角色'),
                                   adventurer_embed=lambda *args: discord.Embed(title='冒險者名片'))
        self.interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                     defer=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = AdventureView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_help_topics_switch_in_place_and_refresh_keeps_selection(self):
        await self.view.handle(self.interaction, 'help')
        guide = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(guide.stop)
        self.assertIn('新手入門', guide.embed().title)
        self.assertNotIn('三次方根', guide.embed().description)
        for topic, (label, _) in HELP_TOPICS.items():
            select = next(child for child in guide.children if isinstance(child, discord.ui.Select))
            select._values = [topic]
            await select.callback(self.interaction)
            self.assertIs(self.interaction.response.edit_message.call_args.kwargs['view'], guide)
            embed = guide.embed()
            self.assertIn(label, embed.title)
            self.assertLessEqual(len(embed.description), 4096)
            self.assertLessEqual(len(embed), 6000)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
            self.assertLessEqual(len(guide.to_components()), 5)
            await guide.handle(self.interaction, 'refresh')
            select = next(child for child in guide.children if isinstance(child, discord.ui.Select))
            self.assertEqual([option.value for option in select.options if option.default], [topic])
        await guide.handle(self.interaction, 'help_topic', 'unknown')
        self.assertEqual(guide.help_topic, 'advanced')

    async def test_help_covers_major_systems_with_actionable_directions(self):
        self.assertEqual(set(HELP_TOPICS), {
            'intro', 'growth', 'combat', 'skills', 'raids', 'modes',
            'gathering', 'cooking', 'alchemy', 'town', 'economy', 'advanced'})
        expected_text = {
            'intro': '/邀請', 'growth': '每日基礎上限', 'combat': '出戰配置',
            'skills': '優先 1', 'raids': '/攻略', 'modes': '繪境迷宮',
            'gathering': '釣魚與農耕各有獨立 XP', 'cooking': '成長提高討伐 XP',
            'alchemy': '思考核心', 'town': '占卜室', 'economy': '不能直接轉帳',
            'advanced': '實際命中率',
        }
        for topic, expected in expected_text.items():
            guide = AdventureView(self.cog, self.interaction, 'help', help_topic=topic)
            self.addCleanup(guide.stop)
            embed = guide.embed()
            rendered = embed.description + ''.join(field.value for field in embed.fields)
            self.assertIn(expected, rendered)

    async def test_home_groups_features_and_keeps_utilities_last(self):
        rows = {child.label: child.row for child in self.view.children
                if isinstance(child, discord.ui.Button)}
        self.assertEqual([rows[label] for label in ('角色', '物品', '生活')], [0, 0, 0])
        self.assertEqual([rows[label] for label in ('冒險', '說明')], [1, 1])
        self.assertEqual([rows[label] for label in ('重新整理', '關閉')], [4, 4])

        await self.view.handle(self.interaction, 'character')
        character = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(character.stop)
        labels = [child.label for child in character.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(labels[:5], ['裝備／能力', '技能', '出戰配置', '轉職', '訓練假人'])

    async def test_favorites_are_persistent_and_open_shortcuts(self):
        await self.view.handle(self.interaction, 'character')
        character = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(character.stop)
        toggle = next(child for child in character.children
                      if getattr(child, 'label', '') == '☆ 加入最愛')
        await toggle.callback(self.interaction)
        self.assertEqual(self.store.menu_favorites(1, 1), ('character',))
        self.assertEqual(toggle.label, '★ 移除最愛')

        await character.handle(self.interaction, 'home')
        home = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(home.stop)
        labels = [child.label for child in home.children
                  if isinstance(child, discord.ui.Button)]
        self.assertIn('⭐ 角色', labels)

        self.store.set_menu_favorites(1, 1, [
            'character', 'equipment', 'skills', 'loadouts', 'jobs',
            'training', 'items', 'backpack', 'shop', 'use_items'])
        home = AdventureView(self.cog, self.interaction)
        self.addCleanup(home.stop)
        self.assertEqual(len(home.to_components()), 5)
        self.assertTrue(all(len(row['components']) <= 5 for row in home.to_components()))

        await home.handle(self.interaction, 'equipment')
        panel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(panel.stop)
        self.assertIn('裝備', panel.embed().title)
        self.assertTrue(any(getattr(child, 'label', '') == '★ 移除最愛'
                            for child in panel.children))
        back = next(child for child in panel.children
                    if getattr(child, 'label', '') == '返回首頁')
        await back.callback(self.interaction)
        returned = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(returned.stop)
        self.assertEqual(returned.page, 'home')

    async def test_favorite_toggle_removes_and_enforces_limit(self):
        self.store.set_menu_favorites(1, 1, ('character',))
        character = AdventureView(self.cog, self.interaction, 'character')
        self.addCleanup(character.stop)
        toggle = next(child for child in character.children
                      if getattr(child, 'label', '') == '★ 移除最愛')
        await toggle.callback(self.interaction)
        self.assertEqual(self.store.menu_favorites(1, 1), ())
        self.assertEqual(toggle.label, '☆ 加入最愛')

        self.store.set_menu_favorites(1, 1, list(FAVORITE_PAGES)[:10])
        travel = AdventureView(self.cog, self.interaction, 'travel')
        self.addCleanup(travel.stop)
        toggle = next(child for child in travel.children
                      if getattr(child, 'label', '') == '☆ 加入最愛')
        self.interaction.response.send_message.reset_mock()
        await toggle.callback(self.interaction)
        self.interaction.response.send_message.assert_awaited_once()
        self.assertEqual(len(self.store.menu_favorites(1, 1)), 10)

    async def test_feature_pages_return_to_their_immediate_parent(self):
        jobs = AdventureView(self.cog, self.interaction, 'jobs')
        self.addCleanup(jobs.stop)
        self.assertTrue(any(getattr(child, 'label', '') == '返回角色'
                            for child in jobs.children))
        backpack = AdventureView(self.cog, self.interaction, 'backpack')
        self.addCleanup(backpack.stop)
        self.assertTrue(any(getattr(child, 'label', '') == '返回物品'
                            for child in backpack.children))

        await self.view.handle(self.interaction, 'character')
        character = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(character.stop)
        await character.handle(self.interaction, 'equipment')
        equipment = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(equipment.stop)
        back = next(child for child in equipment.children
                    if getattr(child, 'label', '') == '返回角色')
        await back.callback(self.interaction)
        parent = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(parent.stop)
        self.assertEqual(parent.page, 'character')
        back = next(child for child in parent.children
                    if getattr(child, 'label', '') == '返回首頁')
        await back.callback(self.interaction)
        root = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(root.stop)
        self.assertEqual(root.page, 'home')

        shop = AdventureView(self.cog, self.interaction, 'items')
        self.addCleanup(shop.stop)
        await shop.handle(self.interaction, 'shop')
        shop = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(shop.stop)
        back = next(child for child in shop.children
                    if getattr(child, 'label', '') == '返回物品')
        await back.callback(self.interaction)
        parent = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(parent.stop)
        self.assertEqual(parent.page, 'items')

        raid_items = ItemUseView(self.cog, self.interaction, return_page='raids')
        self.addCleanup(raid_items.stop)
        self.assertTrue(any(getattr(child, 'label', '') == '返回討伐'
                            for child in raid_items.children))

    async def test_each_visible_page_has_an_independent_favorite(self):
        backpack = AdventureView(self.cog, self.interaction, 'backpack')
        self.addCleanup(backpack.stop)
        toggle = next(child for child in backpack.children
                      if getattr(child, 'label', '') == '☆ 加入最愛')
        await toggle.callback(self.interaction)
        self.assertEqual(self.store.menu_favorites(1, 1), ('backpack:全部',))

        await backpack.handle(self.interaction, 'category', '裝備')
        self.assertTrue(any(getattr(child, 'label', '') == '☆ 加入最愛'
                            for child in backpack.children))
        self.assertFalse(any(getattr(child, 'label', '') == '★ 移除最愛'
                             for child in backpack.children))
        self.assertEqual(self.store.menu_favorites(1, 1), ('backpack:全部',))
        back = next(child for child in backpack.children
                    if getattr(child, 'label', '') == '返回背包・全部')
        await back.callback(self.interaction)
        previous = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(previous.stop)
        self.assertEqual((previous.page, previous.category), ('backpack', '全部'))

        home = AdventureView(self.cog, self.interaction)
        self.addCleanup(home.stop)
        await home.handle(self.interaction, 'backpack:全部')
        shortcut = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(shortcut.stop)
        self.assertEqual((shortcut.page, shortcut.category), ('backpack', '全部'))
        self.assertTrue(any(getattr(child, 'label', '') == '返回首頁'
                            for child in shortcut.children))

    async def test_help_topic_rejects_foreign_user_and_closed_panel(self):
        guide = AdventureView(self.cog, self.interaction, 'help')
        self.addCleanup(guide.stop)
        stranger = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        await guide.handle(stranger, 'help_topic', 'advanced')
        self.assertEqual(guide.help_topic, 'intro')
        stranger.response.edit_message.assert_not_awaited()
        await guide.handle(self.interaction, 'close')
        await guide.handle(self.interaction, 'help_topic', 'advanced')
        self.assertEqual(guide.help_topic, 'intro')

    async def test_paused_xp_notice_is_visible_on_intro_and_growth(self):
        self.cog.settings = replace(self.cog.settings, enabled=False)
        guide = AdventureView(self.cog, self.interaction, 'help')
        self.addCleanup(guide.stop)
        for topic in ('intro', 'growth'):
            await guide.handle(self.interaction, 'help_topic', topic)
            self.assertTrue(any('暫停聊天與語音經驗' in field.value for field in guide.embed().fields))

    async def test_stale_view_and_timeout_cannot_overwrite_new_page(self):
        await self.view.handle(self.interaction, 'help')
        child = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(child.stop)
        self.interaction.response.edit_message.reset_mock()
        await self.view.on_timeout()
        self.interaction.edit_original_response.assert_not_awaited()
        await self.view.handle(self.interaction, 'jobs')
        self.interaction.response.edit_message.assert_not_awaited()
        await child.on_timeout()
        self.interaction.edit_original_response.assert_awaited_once()
        await child.handle(self.interaction, 'home')
        self.interaction.response.edit_message.assert_not_awaited()

    async def test_foreign_user_and_guild_cannot_navigate_or_change_jobs(self):
        for guild, user in ((1, 2), (2, 1)):
            stranger = SimpleNamespace(guild_id=guild, user=SimpleNamespace(id=user),
                response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
            await self.view.handle(stranger, 'jobs')
            stranger.response.edit_message.assert_not_awaited()
            stranger.response.send_message.assert_awaited_once()
        self.assertFalse(self.view.closed)

    async def test_backpack_pagination_bounds_and_component_rows(self):
        self.store.award_voice([(1, 1, 200000000)])
        for job in JOBS:
            self.characters.change_job(1, 1, job)
        await self.view.handle(self.interaction, 'backpack')
        bag = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(bag.stop)
        self.assertIn('背包 1/2', bag.embed().title)
        await bag.handle(self.interaction, 'next')
        self.assertIn('背包 2/2', bag.embed().title)
        await bag.handle(self.interaction, 'next')
        self.assertEqual(bag.index, 1)
        await bag.handle(self.interaction, 'previous')
        await bag.handle(self.interaction, 'previous')
        self.assertEqual(bag.index, 0)
        self.assertLessEqual(len(bag.to_components()), 5)

    async def test_backpack_uses_single_extensible_item_action_panel(self):
        for key in ('paint:red', 'paint:yellow', 'paint:blue', 'noah:unfinished',
                    'painting:balloon'):
            self.characters.grant_item(1, 1, key)
        await self.view.handle(self.interaction, 'backpack')
        bag = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(bag.stop)
        labels = [child.label for child in bag.children if isinstance(child, discord.ui.Button)]
        self.assertIn('使用道具', labels)
        self.assertNotIn('組合噴漆罐', labels)
        self.assertNotIn('使用噴漆罐套組', labels)

        self.interaction.response.edit_message.reset_mock()
        await bag.handle(self.interaction, 'use_items')
        panel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(panel.stop)
        self.assertEqual(panel.catalog,
                         ['recipe:paint_set', 'noah:unfinished', 'painting:balloon'])
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['paint:set'], 1)
        await panel.handle(self.interaction, 'select', 'noah:unfinished')
        await panel.handle(self.interaction, 'use')
        self.assertEqual(self.characters.inventory_counts(1, 1)['noah:unfinished'], 1)
        self.cog.painted_maze.create.assert_awaited_once_with(
            self.interaction, 'noah:unfinished')
        notice = self.interaction.edit_original_response.call_args.kwargs['embed'].fields[0].value
        self.assertIn('繪境迷宮 #7', notice)
        self.assertIn('開始探索時消耗', notice)

        self.characters.grant_item(1, 1, 'maze:choice_box:archer')
        panel.rebuild()
        await panel.handle(self.interaction, 'select', 'maze:choice_box:archer')
        await panel.handle(self.interaction, 'choose:weapon')
        self.assertEqual(self.characters.inventory_counts(1, 1).get(
            'maze:choice_box:archer', 0), 1)
        await panel.handle(self.interaction, 'confirm_choice')
        self.assertEqual(self.characters.inventory_counts(1, 1).get(
            'maze:choice_box:archer', 0), 0)
        self.assertTrue(any(entry.item_id == 'maze:archer:weapon'
                            for entry in self.characters.inventory_entries(1, 1)))

    async def test_public_profile_opens_private_showcase_settings(self):
        self.characters.grant_item(1, 1, 'paint:red')
        card = ProfileCardView(self.cog, 1)
        self.addCleanup(card.stop)
        await card.children[0].callback(self.interaction)
        profile = self.interaction.response.send_message.call_args.kwargs['view']
        self.addCleanup(profile.stop)
        await profile.handle(self.interaction, 'showcase', 'paint:red')
        self.assertEqual(self.characters.showcase(1, 1), 'paint:red')
        self.assertIn('現在展示', self.interaction.response.edit_message.call_args.kwargs['embed'].fields[-1].value)
        await profile.handle(self.interaction, 'clear')
        self.assertIsNone(self.characters.showcase(1, 1))

    async def test_movement_page_opens_mag_divination_room(self):
        labels = [child.label for child in self.view.children if isinstance(child, discord.ui.Button)]
        self.assertIn('冒險', labels)
        await self.view.handle(self.interaction, 'travel')
        travel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(travel.stop)
        self.assertIn('冒險', travel.embed().title)
        await travel.handle(self.interaction, 'divination')
        room = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(room.stop)
        self.assertIn('瑪格的占卜室', room.embed().title)
        self.assertIn('免費', room.embed().description)
        self.assertIn('0 金幣', room.embed().description)
        labels = [child.label for child in room.children if isinstance(child, discord.ui.Button)]
        self.assertIn('返回冒險', labels)
        await room.handle(self.interaction, 'travel')
        travel = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(travel.stop)
        self.assertIn('冒險', travel.embed().title)

    async def test_existing_divination_requires_confirmation_before_paid_reveal(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,1000)')
        room = DivinationView(self.cog, self.interaction)
        self.addCleanup(room.stop)
        await room.handle(self.interaction, 'reveal')
        self.assertEqual(self.divinations.status(1, 1)['draws'], 1)
        offer = self.divinations.status(1, 1)['offer']
        await room.handle(self.interaction, 'choose', offer[0])

        await room.handle(self.interaction, 'reveal')
        self.assertEqual(self.divinations.status(1, 1)['draws'], 1)
        self.assertTrue(any(getattr(child, 'label', '').startswith('確認揭牌')
                            for child in room.children))
        await room.handle(self.interaction, 'reveal_confirm')
        self.assertEqual(self.divinations.status(1, 1)['draws'], 2)
        self.assertEqual(self.divinations.status(1, 1)['card'], offer[0])
