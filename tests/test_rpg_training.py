from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

import discord

from core.rpg import RPGStore
from core.rpg_battle import Rule, Tactics, raid_battle
from core.rpg_character import Characters, CharacterError
from core.rpg_menu import AdventureView
from core.rpg_training import train
from core.rpg_training_view import TrainingView
from core.settings import RPGSettings


def participant(job='弓兵', rules=None):
    return dict(id=1, name='測試者', state=dict(
        job=job, level=30, speed=50, total=[50] * 5,
        combat={'HP': 1000, '攻擊': 200, '防禦': 50, '治療量': 100,
                '命中率': 100, '閃避率': 0, '暴擊率': 0},
        equipped={'武器': 'test'}, crystal_effects=[
            dict(effects=['opening_shield_percent'], values=[20])]),
        rules=[asdict(rule) for rule in (rules or [])], passive_id=1)


class TrainingTests(unittest.TestCase):
    def test_full_duration_fresh_randomness_and_input_isolation(self):
        source = participant(rules=[Rule(1, 1, True, 'always', 'lowest', 5)])
        before = deepcopy(source)
        battle, damage = train(source, rounds=30)
        repeated, _ = train(source, rounds=30)
        self.assertEqual(source, before)
        self.assertEqual(battle.round, 30)
        self.assertNotEqual(battle.rng.getstate(), repeated.rng.getstate())
        actor, dummy = battle.fighters
        self.assertEqual(actor.hp, actor.stats['HP'])
        self.assertEqual(actor.combat_stats['damage_taken'], 0)
        self.assertGreater(actor.combat_stats['direct_damage'], 0)
        self.assertEqual(actor.combat_stats['support_damage'], 0)
        self.assertEqual(sum(damage), actor.combat_stats['damage_dealt'])
        self.assertEqual(sum(damage), dummy.combat_stats['damage_taken'])
        self.assertEqual(dummy.hp, dummy.stats['HP'])
        self.assertEqual(dummy.combat_stats['attacks'], 0)

    def test_aoe_and_defense(self):
        source = participant(rules=[Rule(1, 1, True, 'always', 'lowest', 3)])
        single, _ = train(source)
        group, _ = train(source, count=3)
        armored, _ = train(source, defense=1000)
        self.assertGreater(group.fighters[0].combat_stats['damage_dealt'],
                           single.fighters[0].combat_stats['damage_dealt'])
        self.assertLess(armored.fighters[0].combat_stats['damage_dealt'],
                        single.fighters[0].combat_stats['damage_dealt'])
        self.assertTrue(all(f.combat_stats['damage_taken'] > 0 for f in group.fighters[1:]))

    def test_dummy_does_not_cap_lethal_damage_or_count_deaths(self):
        source = participant()
        source['state']['combat']['攻擊'] = 10**9
        battle, damage = train(source, rounds=5)
        self.assertTrue(all(value > 1_000_000 for value in damage))
        self.assertEqual(battle.fighters[1].combat_stats['deaths'], 0)
        self.assertEqual(battle.fighters[0].combat_stats['knockouts'], 0)

    def test_same_equipment_and_passive_initialization_as_raid(self):
        source = participant()
        training, _ = train(source)
        raid = raid_battle([source], {'kind': '史萊姆', 'name': '史萊姆', 'balance_version': 3}, 1)
        actual, expected = training.fighters[0], raid.fighters[0]
        self.assertEqual(actual.stats, expected.stats)
        self.assertEqual(actual.passive_id, expected.passive_id)
        self.assertEqual(actual.status_stacks['crystal_shield'], expected.status_stacks['crystal_shield'])

    def test_invalid_settings(self):
        for options in ({'rounds': 0}, {'rounds': 1000}, {'count': 2},
                        {'count': True}, {'defense': -1}):
            with self.assertRaises(CharacterError):
                train(participant(), **options)


class TrainingViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(store.close)
        self.cog = SimpleNamespace(characters=Characters(store, RPGSettings()),
                                   tactics=Tactics(store), menu_views=set())
        self.store = store
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=1, display_name='玩家'), guild_id=1,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())
        self.view = TrainingView(self.cog, self.interaction)
        self.addCleanup(self.view.stop)

    async def test_navigation_layout_and_owner_guard(self):
        home = AdventureView(self.cog, self.interaction)
        self.addCleanup(home.stop)
        self.assertIn('訓練假人', [c.label for c in home.children if isinstance(c, discord.ui.Button)])
        await home.handle(self.interaction, 'training')
        opened = self.interaction.response.edit_message.call_args.kwargs['view']
        self.addCleanup(opened.stop)
        self.assertIsInstance(opened, TrainingView)
        self.assertLessEqual(len(opened.to_components()), 5)
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), guild_id=1,
            response=SimpleNamespace(send_message=AsyncMock()))
        await self.view.handle(stranger, 'run')
        self.assertIsNone(self.view.battle)
        stranger.response.send_message.assert_awaited_once()

    async def test_run_preserves_database_and_setting_change_clears_results(self):
        self.cog.characters.snapshot(1, 1)
        self.cog.tactics.rules(1, 1, '民兵')
        self.cog.tactics.passive(1, 1, '民兵')
        self.cog.tactics.basic_target(1, 1, '民兵')
        before = list(self.cog.characters.db.iterdump())
        await self.view.handle(self.interaction, 'run')
        self.assertEqual(before, list(self.cog.characters.db.iterdump()))
        self.assertEqual(self.view.battle.round, 10)
        self.assertIn('測試結果', self.view.embed().fields[0].name)
        await self.view.handle(self.interaction, 'log')
        self.interaction.response.send_message.assert_awaited_once()
        await self.view.handle(self.interaction, 'defense', '300')
        self.assertIsNone(self.view.battle)
        self.assertEqual(self.view.defense, 300)
        self.view.closed = True
        await self.view.handle(self.interaction, 'run')
        self.assertIsNone(self.view.battle)
