import random
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

import discord

from core.rpg import RPGStore
from core.rpg_character import Characters, ITEMS, add_owned_item
from core.rpg_witch_rest import (
    ENTRY_PROOFS,
    WitchRestStore,
    equipment_id,
    party_size_allowed,
    reroll_cost,
    roll_affixes,
    roll_reward,
    treasure_weights,
)
from core.rpg_total_battle import dump_total_battle, load_total_battle
from core.rpg_witch_rest_battle import (
    WitchRestAutoBattle,
    WitchRestManualBattle,
    auto_battle_from_participants,
    manual_battle_from_participants,
    manual_round_limit,
    manual_stats,
    run_auto_battle,
)
from core.rpg_witch_rest_service import WitchRestService
from tests.test_rpg_total_raids import FakeBot, FakeChannel, FakeThread, HashableMember


class WitchRestRulesTests(unittest.TestCase):
    @staticmethod
    def participant(user_id=1, attack=100_000):
        return {'id': user_id, 'name': f'P{user_id}', 'state': {
            'level': 80, 'job': '裝甲步兵',
            'combat': {'HP': 10_000, '攻擊': attack, '防禦': 1_000, '治療量': 0,
                       '命中率': 100, '閃避率': 0, '暴擊率': 0},
            'speed': 80, 'stability': (100, 100), 'equipped': {'武器': 'test'}},
            'rules': [], 'basic_target': 'boss', 'passive_id': None}

    def test_party_boundary_switches_at_one_hundred(self):
        self.assertTrue(party_size_allowed(99, 1))
        self.assertFalse(party_size_allowed(100, 1))
        self.assertTrue(party_size_allowed(100, 3))
        self.assertFalse(party_size_allowed(100, 7))
        self.assertEqual(ENTRY_PROOFS, 10)

    def test_low_enrage_cannot_roll_t90_material_or_directed_memory(self):
        kinds = {kind for kind, _ in treasure_weights(99)}
        self.assertNotIn('crystal', kinds)
        self.assertNotIn('directed_memory', kinds)
        for seed in range(200):
            reward = roll_reward(random.Random(seed), 'ema', '弓兵', 99)
            self.assertNotIn('witch_rest:crystal_shard', reward['items'])
            self.assertNotIn(reward['treasure'], {
                'witch_rest:crystal', 'witch_rest:ema:directed_memory'})

    def test_affixes_follow_slot_and_degree_pools(self):
        for seed in range(100):
            prefix, suffix = roll_affixes(random.Random(seed), '裝甲步兵', 'weapon', 4000)
            self.assertIn(prefix[0], {'vitality', 'assault', 'fortitude'})
            self.assertIn(suffix[0], {'precision', 'haste', 'critical', 'prowess', 'stability', 'drain'})
            self.assertIn(prefix[1], {3, 4})
            self.assertIn(suffix[1], {3, 4})

    def test_reroll_cost_keeps_rising(self):
        self.assertEqual(reroll_cost(0), {'fragment': 4, 'dust': 2, 'gold': 1500})
        self.assertEqual(reroll_cost(6), {'fragment': 16, 'dust': 8, 'gold': 10500})

    def test_static_first_release_equipment_is_registered(self):
        item = ITEMS[equipment_id('hiro', '僧侶', 'weapon', 90)]
        self.assertEqual(item.required_level, 90)
        self.assertEqual(item.combat, (0, 217, 0, 231))
        self.assertEqual(item.sell_price, 12000)
        self.assertEqual(ITEMS['witch_rest:ema:accessory'].embroidery_slots, 3)
        self.assertTrue(ITEMS['witch_rest:ema:directed_memory'].transferable)
        self.assertTrue(ITEMS['witch_rest:memory_page'].transferable)
        self.assertFalse(ITEMS['witch_rest:advanced_memory'].transferable)

    def test_auto_hiro_uses_one_weak_rewind(self):
        battle = run_auto_battle([self.participant()], 'hiro', 99, seed=7)
        self.assertEqual(battle.result, '勝利')
        self.assertTrue(battle.mechanics['hiro_rewound'])
        self.assertLessEqual(battle.round, 30)

    def test_auto_battle_survives_between_rounds(self):
        battle = auto_battle_from_participants(
            [self.participant(attack=100)], 'ema', 99, seed=7)
        battle.step()
        loaded = load_total_battle(dump_total_battle(battle))
        self.assertIsInstance(loaded, WitchRestAutoBattle)
        self.assertEqual(loaded.round, 1)
        self.assertEqual(loaded.rng.getstate(), battle.rng.getstate())

    def test_manual_stats_interpolate_approved_anchors(self):
        self.assertEqual(manual_stats('ema', 1000), (90_000, 1_625, 500))
        self.assertEqual(manual_stats('ema', 1500), (101_250, 1_772, 550))
        self.assertEqual([manual_round_limit('ema', value)
                          for value in (100, 500, 750, 1_000, 2_000, 4_000)],
                         [30, 17, 17, 22, 29, 30])
        self.assertEqual(manual_round_limit('hiro', 2_000), 30)

    def test_manual_battle_scales_party_and_survives_restart(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        battle = manual_battle_from_participants(participants, 'ema', 1000, seed=7)
        boss = battle.witch('ema')
        self.assertEqual((boss.stats['HP'], boss.stats['攻擊'], boss.stats['防禦']),
                         (90_000, 1_625, 500))
        self.assertEqual(battle.max_rounds, 22)
        loaded = load_total_battle(dump_total_battle(battle))
        self.assertIsInstance(loaded, WitchRestManualBattle)
        self.assertEqual((loaded.witch_id, loaded.enrage), ('ema', 1000))
        self.assertEqual(loaded.rng.getstate(), battle.rng.getstate())

    def test_ema_prosecution_consumes_factors_and_builds_shield(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 4)]
        battle = manual_battle_from_participants(participants, 'ema', 100, seed=7)
        player, boss = battle.living(0)[0], battle.witch('ema')
        for _ in range(3):
            battle.mark(player, 'factor', 30, True)
        boss.hp -= 10_000
        battle.prepare(boss)
        battle.spell(boss, battle.pending.pop('ema'))
        self.assertEqual(player.hp, 6_400)
        self.assertEqual(battle.factor_stacks(player), 0)
        self.assertGreater(battle.mechanics['rest_boss_shield'], 0)
        self.assertGreater(boss.hp, boss.stats['HP'] - 10_000)

    def test_ema_evidence_preserves_factor_and_can_disprove_prosecution(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 4)]
        battle = manual_battle_from_participants(participants, 'ema', 500, seed=7)
        player, boss = battle.living(0)[0], battle.witch('ema')
        for _ in range(3):
            battle.mark(player, 'factor', 30, True)
        player.status_stacks['rest_factor_preservation'] = 1
        battle.clear_negative_effects(player)
        self.assertEqual(battle.factor_stacks(player), 3)
        battle.clear_negative_effects(player)
        self.assertEqual(battle.factor_stacks(player), 0)

        battle.mark(player, 'factor', 30, True)
        battle.prepare(boss)
        data = battle.pending['ema']
        evidence = battle.fighter_for_key(data['objects'][0])
        self.assertEqual(data['objection_resist'], 1)
        self.assertFalse(battle.apply_debuff(boss, 'stun', battle.round + 1))
        self.assertIn('ema', battle.pending)
        evidence.hp = 0
        before = player.hp
        battle.spell(boss, data)
        self.assertEqual(player.hp, before)
        self.assertEqual(battle.factor_stacks(player), 1)

    def test_ema_broken_evidence_still_advances_guilt_cadence(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        for enrage, cadence in ((500, 3), (1_000, 2), (4_000, 1)):
            with self.subTest(enrage=enrage):
                battle = manual_battle_from_participants(participants, 'ema', enrage, seed=7)
                player, boss = battle.living(0)[0], battle.witch('ema')
                for count in range(1, cadence + 1):
                    battle.mark(player, 'factor', 30, True)
                    battle.prepare(boss)
                    data = battle.pending.pop('ema')
                    battle.fighter_for_key(data['objects'][0]).hp = 0
                    battle.spell(boss, data)
                    self.assertEqual(battle.mechanics['ema_prosecutions'], count)
                    self.assertEqual(bool(battle.mechanics.get('ema_guilt_due')),
                                     count == cadence)

    def test_ema_evidence_pressure_scales_and_guilt_precedes_final(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        evidence_hp = []
        for enrage in (500, 750, 1_000, 2_000, 4_000):
            battle = manual_battle_from_participants(participants, 'ema', enrage, seed=7)
            player, boss = battle.living(0)[0], battle.witch('ema')
            battle.mark(player, 'factor', 30, True)
            battle.prepare(boss)
            evidence = battle.fighter_for_key(battle.pending['ema']['objects'][0])
            evidence_hp.append(evidence.stats['HP'])
        self.assertEqual(evidence_hp, sorted(evidence_hp))

        battle = manual_battle_from_participants(participants, 'ema', 500, seed=7)
        player, boss = battle.living(0)[0], battle.witch('ema')
        battle.mark(player, 'factor', 30, True)
        battle.prepare(boss)
        data = battle.pending.pop('ema')
        first_hp = battle.fighter_for_key(data['objects'][0]).stats['HP']
        battle.fighter_for_key(data['objects'][0]).hp = 0
        battle.spell(boss, data)
        self.assertEqual(battle.mechanics['ema_court_authority'], 1)

        battle.prepare(boss)
        data = battle.pending.pop('ema')
        second_hp = battle.fighter_for_key(data['objects'][0]).stats['HP']
        self.assertGreater(second_hp, first_hp)
        battle.spell(boss, data)
        self.assertEqual(battle.mechanics['ema_court_authority'], 0)

        battle = manual_battle_from_participants(participants, 'ema', 1_000, seed=7)
        boss = battle.witch('ema')
        boss.hp = boss.stats['HP'] * 30 // 100
        battle.mechanics['ema_guilt_due'] = True
        battle.act(boss)
        self.assertEqual(battle.pending['ema']['kind'], 'rest_guilt')
        self.assertNotIn('ema_final', battle.mechanics)

    def test_ema_shared_testimony_and_guilt_verdict(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        battle = manual_battle_from_participants(participants, 'ema', 750, seed=7)
        players, boss = battle.living(0), battle.witch('ema')
        boss.hp = boss.stats['HP'] * 60 // 100
        for player in players[:2]:
            battle.mark(player, 'factor', 30, True)
        battle.prepare(boss)
        before = players[1].hp
        battle._rest_player_actor = players[2]
        battle.apply_damage(players[0], 1_000, direct=True)
        battle._rest_player_actor = None
        self.assertEqual(players[1].hp, before - 300)

        battle.pending.pop('ema')
        battle._start_ema_guilt(boss)
        data = battle.pending['ema']
        defendant = battle.fighter_for_key(data['targets'][0])
        defendant.effects['defend'] = battle.round
        data['defended'].append(defendant.user_id)
        object_key = data['objects'][0]
        data['hits'][object_key] = data['assignments'][object_key][:2]
        before = defendant.hp
        battle.spell(boss, data)
        self.assertEqual(defendant.hp, before - defendant.stats['HP'] * 20 // 100)

    def test_ema_guilt_identifies_each_evidence_and_its_advocate(self):
        participants = [self.participant(user_id=user_id, attack=100)
                        for user_id in range(1, 5)]
        battle = manual_battle_from_participants(participants, 'ema', 4000, seed=7)
        battle._start_ema_guilt(battle.witch('ema'))
        data = battle.pending['ema']

        intent = battle.intent()
        for index, target_key in enumerate(data['targets']):
            defendant = battle.fighter_for_key(target_key)
            object_key = data['objects'][index]
            evidence = battle.fighter_for_key(object_key)
            advocate = next(player for player in battle.fighters
                             if player.user_id == data['assignments'][object_key][0])
            self.assertEqual(evidence.name,
                             f'{defendant.name}的斷罪證物（辯護：{advocate.name}）')
            self.assertIn(f'{defendant.name}：尚未防禦｜辯護人 {advocate.name}｜舉證 0/1',
                          intent.description)

    def test_ema_witch_killer_success_and_failure_are_explicit(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        battle = manual_battle_from_participants(participants, 'ema', 1000, seed=7)
        boss = battle.witch('ema')
        battle._start_ema_final(boss)
        final = battle.mechanics['ema_final']
        final['denied'] = list(final['manifests'])
        final['advocated'] = list(final['manifests'])
        for _ in range(3):
            battle.round += 1
            battle._resolve_ema_final_round()
        self.assertFalse(final['active'])
        self.assertEqual(final['manifests'], [])
        self.assertEqual(battle.mechanics['rest_vulnerable_until'], battle.round + 1)

        failed = manual_battle_from_participants(participants, 'ema', 1000, seed=8)
        failed._start_ema_final(failed.witch('ema'))
        for _ in range(3):
            failed.round += 1
            failed._resolve_ema_final_round()
        self.assertFalse(failed.living(0))

    def test_hiro_two_rewinds_and_final_worldline(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        battle = manual_battle_from_participants(participants, 'hiro', 1000, seed=7)
        boss = battle.witch('hiro')
        references = ('defend', 'attack', 'attack', 'attack')
        for player, reference in zip(battle.living(0), references):
            battle.mechanics.setdefault('rest_action_history', {})[str(player.user_id)] = [
                reference, reference, reference]
        boss.hp = 0
        self.assertFalse(battle.check_end())
        self.assertEqual(boss.hp, boss.stats['HP'] * 30 // 100)
        boss.hp = 0
        self.assertFalse(battle.check_end())
        final = battle.mechanics['hiro_final']
        self.assertEqual(boss.hp, 1)
        categories = ('attack', 'skill', 'defend', 'skill')
        for _ in range(3):
            battle.round += 1
            final['current'] = {str(player.user_id): category
                                for player, category in zip(battle.living(0), categories)}
            battle._resolve_hiro_round()
        self.assertFalse(final['active'])
        self.assertEqual(len(battle.living(0)), 4)

    def test_hiro_rewrite_action_is_once_per_player(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 4)]
        battle = manual_battle_from_participants(participants, 'hiro', 1000, seed=7)
        boss = battle.witch('hiro')
        battle.mechanics['hiro_rewinds'] = 1
        boss.hp = 0
        battle.check_end()
        player = battle.living(0)[0]
        battle.submit(player.user_id, 'rewrite_defend')
        battle._resolve_player(player, battle.choices[player.user_id])
        actions = {item['action'] for item in battle.available_actions(player.user_id)}
        self.assertNotIn('rewrite_defend', actions)
        self.assertIn(player.user_id, battle.mechanics['hiro_final']['rewrite_used'])

    def test_manual_thresholds_complete_real_round_loop(self):
        participants = [self.participant(user_id, attack=5_000) for user_id in range(1, 5)]
        for witch_id in ('ema', 'hiro'):
            for enrage in (100, 250, 500, 750, 1_000, 2_000, 4_000):
                with self.subTest(witch_id=witch_id, enrage=enrage):
                    battle = manual_battle_from_participants(participants, witch_id, enrage, seed=7)
                    for user_id in battle.living_player_ids():
                        battle.enable_auto(user_id)
                    while not battle.result:
                        battle.resolve()
                    self.assertIn(battle.result, {'勝利', '戰敗', '平手（達回合上限）'})

    def test_final_mechanic_state_survives_restart(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        ema = manual_battle_from_participants(participants, 'ema', 4000, seed=7)
        ema._start_ema_final(ema.witch('ema'))
        ema.mechanics['ema_final']['denied'].append(1)
        restored = load_total_battle(dump_total_battle(ema))
        self.assertEqual(restored.mechanics['ema_final']['denied'], [1])
        self.assertEqual(len(restored.mechanics['ema_final']['shadows']), 4)

        hiro = manual_battle_from_participants(participants, 'hiro', 2000, seed=8)
        hiro.mechanics['hiro_rewinds'] = 1
        hiro.witch('hiro').hp = 0
        hiro.check_end()
        restored = load_total_battle(dump_total_battle(hiro))
        self.assertTrue(restored.mechanics['hiro_final']['active'])
        self.assertEqual(restored.witch('hiro').hp, 1)

    def test_four_thousand_strengthens_existing_solutions_only(self):
        participants = [self.participant(user_id, attack=100) for user_id in range(1, 5)]
        ema = manual_battle_from_participants(participants, 'ema', 4000, seed=7)
        for player in ema.living(0):
            ema.mark(player, 'factor', 30, True)
        ema.prepare(ema.witch('ema'))
        self.assertEqual(ema.pending['ema']['objection_resist'], 2)
        self.assertEqual(len(ema.pending['ema']['targets']), 4)
        ema.pending.pop('ema')
        ema._start_ema_guilt(ema.witch('ema'))
        self.assertEqual(len(ema.pending['ema']['targets']), 2)

        hiro = manual_battle_from_participants(participants, 'hiro', 4000, seed=8)
        hiro.mechanics['hiro_rewinds'] = 1
        hiro.witch('hiro').hp = 0
        hiro.check_end()
        self.assertEqual(len(hiro.mechanics['hiro_final']['focus']), 2)


class WitchRestStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.store.create_player(1, 10)
        Characters(self.store, SimpleNamespace(stage_levels=(1, 10, 20, 50)))
        self.rest = WitchRestStore(self.store)

    def quantity(self, user_id, item_id):
        row = self.store.db.execute('''SELECT quantity FROM rpg_inventory
            WHERE guild_id=1 AND user_id=? AND item_id=?''', (user_id, item_id)).fetchone()
        return row[0] if row else 0

    def equipment(self, *, tier=80, grades=(2, 3), source='test-equipment'):
        item_id = equipment_id('ema', '騎士', 'weapon', tier)
        with self.store.db:
            instance_id = add_owned_item(self.store.db, 1, 10, item_id)[0]
            self.store.db.execute('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)',
                                  (instance_id, 0, f'witch:vitality:{grades[0]}', 'combat:0', 100))
            self.store.db.execute('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)',
                                  (instance_id, 1, f'witch:drain:{grades[1]}', 'lifesteal', grades[1]))
            self.store.db.execute('INSERT INTO rpg_witch_rest_equipment VALUES (?,?,?,?,?,?)',
                                  (instance_id, 'ema', 0, 0, source, 0))
        return instance_id

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_settlement_is_idempotent_and_updates_progress_once(self):
        first = self.rest.settle('clear-1', 1, 10, 'ema', '騎士', 1000, seed=42)
        second = self.rest.settle('clear-1', 1, 10, 'ema', '騎士', 1000, seed=999)
        self.assertEqual(first, second)
        progress = self.rest.progress(1, 10, 'ema')
        self.assertEqual(progress['total_wins'], 1)
        self.assertEqual(progress['eligible_wins'], 1)
        self.assertEqual(progress['highest_enrage'], 1000)

    def test_auto_clear_does_not_advance_bad_luck_protection(self):
        self.rest.settle('clear-low', 1, 10, 'hiro', '弓兵', 99, seed=1)
        progress = self.rest.progress(1, 10, 'hiro')
        self.assertEqual(progress['eligible_wins'], 0)
        self.assertEqual(progress['dry_wins'], 0)
        self.assertEqual(progress['luck_points'], 0)

    def test_entry_charge_is_all_or_nothing_and_idempotent(self):
        self.store.create_player(1, 11)
        with self.store.db:
            add_owned_item(self.store.db, 1, 10, 'proof:raid', 20)
        with self.assertRaisesRegex(Exception, '討伐之證不足'):
            self.rest.charge_entry('room-1', 1, [10, 11])
        self.assertEqual(self.quantity(10, 'proof:raid'), 20)
        with self.store.db:
            add_owned_item(self.store.db, 1, 11, 'proof:raid', 10)
        self.assertTrue(self.rest.charge_entry('room-1', 1, [10, 11]))
        self.assertFalse(self.rest.charge_entry('room-1', 1, [10, 11]))
        self.assertEqual(self.quantity(10, 'proof:raid'), 10)
        self.assertEqual(self.quantity(11, 'proof:raid'), 0)

    def test_upgrade_keeps_instance_and_affixes(self):
        with self.store.db:
            instance_id = add_owned_item(
                self.store.db, 1, 10, equipment_id('ema', '騎士', 'weapon'))[0]
            self.store.db.execute('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)',
                                  (instance_id, 0, 'witch:vitality:3', 'combat:0', 132))
            self.store.db.execute('INSERT INTO rpg_instance_affixes VALUES (?,?,?,?,?)',
                                  (instance_id, 1, 'witch:drain:3', 'lifesteal', 3))
            self.store.db.execute('INSERT INTO rpg_witch_rest_equipment VALUES (?,?,?,?,?,?)',
                                  (instance_id, 'ema', 2, 4, 'source', 0))
            add_owned_item(self.store.db, 1, 10, 'witch_rest:ema:core', 12)
            add_owned_item(self.store.db, 1, 10, 'witch_rest:crystal', 1)
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,10,20000)')
        result = self.rest.upgrade(1, 10, instance_id)
        retry = self.rest.upgrade(1, 10, instance_id)
        self.assertEqual(result, retry)
        self.assertEqual(result['instance_id'], instance_id)
        self.assertIn(':t90:', result['item_id'])
        rows = self.store.db.execute('''SELECT affix_id FROM rpg_instance_affixes
            WHERE instance_id=? ORDER BY affix_index''', (instance_id,)).fetchall()
        self.assertEqual(rows, [('witch:vitality:3',), ('witch:drain:3',)])
        self.assertEqual(self.quantity(10, 'witch_rest:ema:core'), 0)

    def test_practice_room_starts_without_fee_and_records_no_progress(self):
        room = self.rest.create_room(1, 10, 'ema', 99, practice=True, now=100)
        participant = WitchRestRulesTests.participant(10)
        running = self.rest.start_room(room['id'], 10, [participant], now=101)
        self.assertEqual(running['status'], 'running')
        finished = self.rest.finish_room(room['id'], '勝利', {'rounds': 1, 'log': []}, now=102)
        self.assertTrue(finished['practice'])
        self.assertEqual(self.rest.progress(1, 10, 'ema')['total_wins'], 0)
        self.assertEqual(self.quantity(10, 'proof:raid'), 0)

    def test_completed_room_is_archived_after_one_day(self):
        room = self.rest.create_room(1, 10, 'ema', 99, practice=True, now=100)
        running = self.rest.start_room(
            room['id'], 10, [WitchRestRulesTests.participant(10)], now=101)
        finished = self.rest.finish_room(
            running['id'], '勝利', {'rounds': 1, 'log': []}, now=102)
        self.assertEqual(finished['archive_at'], 86_502)
        self.assertEqual(self.rest.archives_due(now=86_501), [])
        self.assertEqual([item['id'] for item in self.rest.archives_due(now=86_502)], [room['id']])
        self.rest.mark_archived(room['id'])
        self.assertEqual(self.rest.archives_due(now=90_000), [])

    def test_formal_room_charges_when_battle_starts_not_when_created(self):
        with self.store.db:
            add_owned_item(self.store.db, 1, 10, 'proof:raid', 10)
        room = self.rest.create_room(1, 10, 'hiro', 0, now=100)
        self.assertEqual(self.quantity(10, 'proof:raid'), 10)
        self.rest.start_room(room['id'], 10, [WitchRestRulesTests.participant(10)], now=101)
        self.assertEqual(self.quantity(10, 'proof:raid'), 0)

    def test_same_clear_can_drop_equipment_for_multiple_players(self):
        self.store.create_player(1, 11)
        seed = next(seed for seed in range(10_000)
                    if (roll_reward(random.Random(seed), 'ema', '騎士', 1000)['treasure'] or '').endswith(
                        (':weapon', ':suit')))
        first = self.rest.settle('party-clear', 1, 10, 'ema', '騎士', 1000, seed=seed)
        second = self.rest.settle('party-clear', 1, 11, 'ema', '騎士', 1000, seed=seed)
        self.assertIsNotNone(first.get('instance_id'))
        self.assertIsNotNone(second.get('instance_id'))
        self.assertNotEqual(first['instance_id'], second['instance_id'])

    def test_reroll_charges_once_and_accepts_preview(self):
        instance_id = self.equipment(source='reroll')
        with self.store.db:
            add_owned_item(self.store.db, 1, 10, 'witch_rest:fragment', 20)
            add_owned_item(self.store.db, 1, 10, 'witch_rest:dust', 10)
            self.store.db.execute('INSERT OR REPLACE INTO rpg_wallets VALUES (1,10,10000)')
        offer = self.rest.offer_reroll('reroll-1', 1, 10, instance_id, 0)
        retry = self.rest.offer_reroll('reroll-1', 1, 10, instance_id, 0)
        self.assertEqual(offer, retry)
        self.assertNotEqual(offer['old'][0], offer['new'][0])
        result = self.rest.resolve_reroll('reroll-1', True)
        self.assertTrue(result['accepted'])
        row = self.store.db.execute('''SELECT affix_id FROM rpg_instance_affixes
            WHERE instance_id=? AND affix_index=0''', (instance_id,)).fetchone()
        self.assertIn(offer['new'][0], row[0])
        self.assertEqual(self.quantity(10, 'witch_rest:fragment'), 16)

    def test_memories_upgrade_and_direct_affixes_idempotently(self):
        instance_id = self.equipment(grades=(1, 2), source='memories')
        with self.store.db:
            add_owned_item(self.store.db, 1, 10, 'witch_rest:advanced_memory', 1)
            add_owned_item(self.store.db, 1, 10, 'witch_rest:ema:directed_memory', 1)
            self.store.db.execute('INSERT OR REPLACE INTO rpg_wallets VALUES (1,10,20000)')
        improved = self.rest.improve_affix('improve-1', 1, 10, instance_id, 0)
        self.assertTrue(improved['success'])
        self.assertEqual(improved['grade'], 2)
        self.assertEqual(self.quantity(10, 'witch_rest:advanced_memory'), 0)
        directed = self.rest.direct_affix('direct-1', 1, 10, instance_id, 1, 'haste')
        retry = self.rest.direct_affix('direct-1', 1, 10, instance_id, 1, 'haste')
        self.assertEqual(directed, retry)
        row = self.store.db.execute('''SELECT affix_id FROM rpg_instance_affixes
            WHERE instance_id=? AND affix_index=1''', (instance_id,)).fetchone()
        self.assertEqual(row[0], 'witch:haste:2')

        failed_instance = self.equipment(grades=(3, 2), source='memory-failure')
        with self.store.db:
            add_owned_item(self.store.db, 1, 10, 'witch_rest:advanced_memory', 1)
        request_id = next(f'improve-fail-{value}' for value in range(100)
                          if random.Random(f'improve-fail-{value}').random() >= .5)
        failed = self.rest.improve_affix(request_id, 1, 10, failed_instance, 0)
        retry = self.rest.improve_affix(request_id, 1, 10, failed_instance, 0)
        self.assertEqual(failed, retry)
        self.assertFalse(failed['success'])
        self.assertEqual(self.quantity(10, 'witch_rest:advanced_memory'), 1)

    def test_t90_dismantle_returns_core_and_pages_but_no_crystal(self):
        instance_id = self.equipment(tier=90, grades=(3, 4), source='dismantle')
        result = self.rest.dismantle('dismantle-1', 1, 10, instance_id)
        retry = self.rest.dismantle('dismantle-1', 1, 10, instance_id)
        self.assertEqual(result, retry)
        self.assertEqual((result['cores'], result['pages']), (8, 3))
        self.assertEqual(self.quantity(10, 'witch_rest:ema:core'), 8)
        self.assertEqual(self.quantity(10, 'witch_rest:memory_page'), 3)
        self.assertEqual(self.quantity(10, 'witch_rest:crystal'), 0)


class WitchRestThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.rest = WitchRestStore(self.store)
        self.bot = FakeBot()
        self.host = HashableMember(10, '房主')
        self.guild = SimpleNamespace(id=1)
        self.parent = FakeChannel(80, self.guild)
        self.thread = FakeThread(81, self.guild)
        self.parent.create_thread.return_value = self.thread
        total_raids = SimpleNamespace(
            repo=SimpleNamespace(active=lambda: []),
            witch_announcement_channels=AsyncMock(return_value=[self.parent]))
        self.cog = SimpleNamespace(
            bot=self.bot, store=self.store, witch_rest=self.rest, total_raids=total_raids)
        self.service = WitchRestService(self.cog)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_create_room_uses_public_card_and_private_thread(self):
        interaction = SimpleNamespace(
            guild_id=1, guild=self.guild, user=self.host)
        with patch('core.rpg_witch_rest_service.level_for', return_value=70):
            room, created = await self.service.create_room(interaction, 'ema', 100)
        self.assertIs(created, self.thread)
        self.assertEqual(room['channel_id'], self.thread.id)
        self.assertEqual(room['parent_channel_id'], self.parent.id)
        self.assertEqual(room['index_message_id'], self.parent.message.id)
        self.parent.send.assert_awaited_once()
        self.parent.create_thread.assert_awaited_once()
        self.thread.add_user.assert_awaited_once_with(self.host)
        self.thread.send.assert_awaited_once()

    async def test_auto_battle_advances_one_round_and_keeps_thread_open(self):
        participant = WitchRestRulesTests.participant(10)
        room = self.rest.create_room(1, 10, 'ema', 99, practice=True, now=100)
        room = self.rest.attach_room(room['id'], self.thread.id, self.thread.message.id)
        room = self.rest.start_room(room['id'], 10, [participant], now=101)
        battle = auto_battle_from_participants([participant], 'ema', 99, seed=7)
        room.update(battle=dump_total_battle(battle), round_deadline=102)
        self.rest.save(room)
        self.bot.channels[self.thread.id] = self.thread
        with patch('core.rpg_witch_rest_service.discord.Thread', FakeThread):
            await self.service._step_auto(room, battle)
        saved = self.rest.get(room['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['battle']['rounds'], 1)
        self.assertIn('fighters', saved['battle'])
        self.assertTrue(saved['report_message_id'])
        self.assertGreater(saved['archive_at'], saved['finished_at'])
        self.thread.edit.assert_not_awaited()
        self.thread.message.edit.assert_awaited_once()
        self.thread.send.assert_awaited_once()
        report = self.thread.send.call_args.kwargs['file'].fp.getvalue().decode('utf-8')
        self.assertIn('完整逐回合記錄：', report)
        self.assertIn('戰鬥結算：', report)
        self.assertIn('獎勵：', report)
        await self.service._post_battle_report(saved)
        self.thread.send.assert_awaited_once()

    async def test_offline_manual_players_are_not_immediately_switched_to_auto(self):
        members = {user_id: HashableMember(user_id) for user_id in (10, 11, 12)}
        for member in members.values():
            member.status = discord.Status.offline
        self.guild.get_member = members.get
        participants = [WitchRestRulesTests.participant(user_id) for user_id in members]
        room = self.rest.create_room(1, 10, 'ema', 100, practice=True)
        self.rest.change_member(room['id'], 11, 80)
        self.rest.change_member(room['id'], 12, 80)
        room = self.rest.attach_room(room['id'], self.thread.id, self.thread.message.id)
        room = self.rest.start_room(room['id'], 10, participants)
        battle = manual_battle_from_participants(participants, 'ema', 100, seed=7)
        room.update(boss='魔女試煉', battle=dump_total_battle(battle),
                    round_deadline=10**20, action_drafts={}, surrender_votes=[])
        self.rest.save(room)
        self.bot.channels[self.thread.id] = self.thread

        with patch('core.rpg_witch_rest_service.discord.Thread', FakeThread):
            await self.service.tick()

        saved = load_total_battle(self.rest.get(room['id'])['battle'])
        self.assertEqual(saved.auto_players, set())
        self.assertEqual(saved.waiting_player_ids(), {10, 11, 12})

    async def test_all_auto_manual_battle_advances_before_deadline(self):
        participants = [WitchRestRulesTests.participant(user_id, attack=100)
                        for user_id in (10, 11, 12)]
        room = self.rest.create_room(1, 10, 'ema', 100, practice=True)
        self.rest.change_member(room['id'], 11, 80)
        self.rest.change_member(room['id'], 12, 80)
        room = self.rest.attach_room(room['id'], self.thread.id, self.thread.message.id)
        room = self.rest.start_room(room['id'], 10, participants)
        battle = manual_battle_from_participants(participants, 'ema', 100, seed=7)
        for fighter in battle.living(0):
            fighter.hp = fighter.stats['HP'] = 1_000_000
            battle.enable_auto(fighter.user_id)
        room.update(boss='魔女試煉', battle=dump_total_battle(battle),
                    round_deadline=time.time() + 120, action_drafts={}, surrender_votes=[])
        self.rest.save(room)
        self.bot.channels[self.thread.id] = self.thread

        with patch('core.rpg_witch_rest_service.discord.Thread', FakeThread):
            await self.service.tick()
            await self.service.tick()

        saved = self.rest.get(room['id'])
        battle = load_total_battle(saved['battle'])
        self.assertEqual(battle.round, 2)
        self.assertEqual(battle.auto_players, {10, 11, 12})
        self.assertGreater(saved['round_deadline'], time.time())

    async def test_offline_players_still_receive_victory_rewards(self):
        Characters(self.store, SimpleNamespace(stage_levels=(1, 10, 20, 50)))
        self.store.create_player(1, self.host.id)
        self.host.status = discord.Status.offline
        self.guild.get_member = lambda user_id: self.host if user_id == self.host.id else None
        self.bot.channels[self.thread.id] = self.thread
        participant = WitchRestRulesTests.participant(self.host.id)
        room = {
            'id': 'offline-clear', 'guild_id': 1, 'channel_id': self.thread.id,
            'witch_id': 'ema', 'enrage': 100, 'practice': False,
            'result': '勝利', 'participants': [participant],
        }

        with patch('core.rpg_witch_rest_service.discord.Thread', FakeThread):
            rewards = self.service._settle_rewards(room)

        self.assertEqual([user_id for user_id, _reward in rewards], [self.host.id])
        saved = self.store.db.execute(
            'SELECT 1 FROM rpg_witch_rest_rewards WHERE clear_id=? AND user_id=?',
            (room['id'], self.host.id)).fetchone()
        self.assertIsNotNone(saved)

    async def test_missing_rewards_are_backfilled_once(self):
        Characters(self.store, SimpleNamespace(stage_levels=(1, 10, 20, 50)))
        self.store.create_player(1, self.host.id)
        with self.store.db:
            add_owned_item(self.store.db, 1, self.host.id, 'proof:raid', 10)
        participant = WitchRestRulesTests.participant(self.host.id)
        room = self.rest.create_room(1, self.host.id, 'ema', 99)
        room = self.rest.attach_room(room['id'], self.thread.id, self.thread.message.id)
        room = self.rest.start_room(room['id'], self.host.id, [participant])
        self.rest.finish_room(room['id'], '勝利', {'rounds': 1, 'log': []})

        self.assertEqual([item['id'] for item in self.rest.rooms_missing_rewards()], [room['id']])
        self.service._backfill_missing_rewards()
        self.assertEqual(self.rest.rooms_missing_rewards(), [])
        saved = self.rest.get(room['id'])
        self.assertEqual([user_id for user_id, _reward in saved['rewards']], [self.host.id])
        self.service._backfill_missing_rewards()
        count = self.store.db.execute(
            'SELECT count(*) FROM rpg_witch_rest_rewards WHERE clear_id=? AND user_id=?',
            (room['id'], self.host.id)).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_result_embed_highlights_treasure_drops(self):
        room = {
            'result': '勝利', 'practice': False, 'witch_id': 'ema', 'enrage': 100,
            'battle': {'rounds': 3, 'log': []},
        }
        reward = {'items': {}, 'gold': 0, 'treasure': 'witch_rest:crystal'}

        embed = self.service.result_embed(room, [(self.host.id, reward)])

        reward_field = next(field for field in embed.fields if field.name == '個人獎勵')
        self.assertIn('**秘寶：凝聚魔女結晶**', reward_field.value)


if __name__ == '__main__':
    unittest.main()
