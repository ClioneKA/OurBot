from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_character import (Characters, CharacterError, GROWTH, JOBS, ITEMS,
                                combat_from_stats, item_sellable)
from core.settings import RPGSettings, SettingsError


class CharacterTests(unittest.TestCase):
    def test_tier_three_items_require_level_thirty_and_snapshot_effects(self):
        from core.rpg_character import item_text
        self.level(10)
        self.characters.change_job(1, 1, '弓兵')
        for key in ('clock:archer', 'plague:bow', 'puppet:twin_charm'):
            with self.store.db:
                self.store.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,?)', (key,))
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, key)
        self.level(30)
        self.characters.equip(1, 1, 'clock:archer')
        self.characters.equip(1, 1, 'plague:bow')
        self.characters.equip(1, 1, 'puppet:twin_charm')
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['damage_guard_chance'], 5)
        self.assertEqual((state['vulnerable_chance'], state['vulnerable_percent']), (5, 10))
        self.assertEqual(state['healing_share'], 10)
        self.assertEqual(ITEMS['puppet:twin_charm'].stats, (2, 2, 2, 2, 2))
        self.assertIn('額外治療', item_text(ITEMS['puppet:twin_charm']))
        self.assertFalse(item_sellable(ITEMS['paint:red']))
        self.assertTrue(ITEMS['paint:red'].transferable)

    def test_fox_pendant_and_bat_weapons(self):
        from core.rpg_character import item_text
        from core.rpg_battle import raid_battle, dump_battle, load_battle
        self.level(10)
        self.characters.change_job(1, 1, '弓兵')
        self.assertFalse(any(k.startswith(('fox:', 'bat:')) for k in self.characters.inventory(1, 1)))
        for key in ('fox:pendant', 'bat:bow'):
            with self.store.db:
                self.store.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,?)', (key,))
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, key)
        self.level(20)
        before = self.characters.snapshot(1, 1)
        self.characters.equip(1, 1, 'fox:pendant')
        self.characters.equip(1, 1, 'bat:bow')
        after = self.characters.snapshot(1, 1)
        self.assertEqual(after['combat']['閃避率'], before['combat']['閃避率'] + 5)
        self.assertEqual(after['lifesteal'], 3)
        battle = raid_battle([dict(name='玩家', state=after, rules=[])], dict(kind='巨獸', name='巨獸'), 1)
        self.assertEqual(load_battle(dump_battle(battle)).fighters[0].lifesteal, 3)
        self.characters.unequip(1, 1, '武器')
        self.characters.unequip(1, 1, '飾品1')
        self.assertEqual(self.characters.snapshot(1, 1)['lifesteal'], 0)
        self.assertEqual(self.characters.snapshot(1, 1)['combat']['閃避率'], before['combat']['閃避率'])
        for key in ('axe', 'sword_shield', 'bow', 'staff'):
            item = ITEMS['bat:' + key]
            self.assertEqual((item.required_level, item.lifesteal, item.price), (20, 3, 0))
            self.assertGreater(item.combat[1], ITEMS[f'{item.job}:1:武器'].combat[1])
        self.assertIn('閃避率 +5%', item_text(ITEMS['fox:pendant']))

    def test_accuracy_can_exceed_100_and_caps_at_150(self):
        from core.rpg_character import combat_from_stats
        for dexterity, accuracy in ((120, 99), (125, 100), (130, 101), (374, 149), (375, 150), (500, 150)):
            with self.subTest(dexterity=dexterity):
                self.assertEqual(combat_from_stats((10, 10, 10, dexterity, 10))['命中率'], accuracy)

    def test_goblin_loot_level_jobs_and_no_free_supplies(self):
        from core.rpg_character import item_text
        self.level(10)
        self.characters.change_job(1, 1, '弓兵')
        self.assertFalse(any(key.startswith('goblin:') for key in self.characters.inventory(1, 1)))
        for key in ('goblin:badge', 'goblin:bow', 'goblin:axe'):
            with self.store.db:
                self.store.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,?)', (key,))
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, key)
        self.level(20)
        self.characters.equip(1, 1, 'goblin:badge')
        self.characters.equip(1, 1, 'goblin:bow')
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['stability'], (60, 140))
        self.assertEqual(state['equipped']['飾品1'], 'goblin:badge')
        with self.assertRaises(CharacterError):
            self.characters.equip(1, 1, 'goblin:axe')
        with self.assertRaises(CharacterError):
            self.characters.buy(1, 1, 'goblin:bow')
        for key in ('axe', 'sword_shield', 'bow', 'staff'):
            item = ITEMS['goblin:' + key]
            self.assertEqual(sum(item.stability) / 2, 100)
            self.assertEqual(item.required_level, 20)
            self.assertGreater(item.combat[1], ITEMS[f'{item.job}:1:武器'].combat[1])
        self.assertIn('整場固定', item_text(ITEMS['goblin:badge']))
        self.level(19)
        self.assertNotIn('goblin:badge', self.characters.snapshot(1, 1)['equipped'].values())

    def test_golem_ranged_weapons_require_job_and_regular_stage(self):
        for job, key in (('弓兵', 'golem:bow'), ('僧侶', 'golem:staff')):
            self.level(10)
            self.characters.change_job(1, 1, job)
            with self.store.db:
                self.store.db.execute('INSERT INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,?)', (key,))
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, key)
            self.level(20)
            self.characters.equip(1, 1, key)
            state = self.characters.snapshot(1, 1)
            self.assertEqual(state['equipped']['武器'], key)
            self.assertEqual(state['stability'], ITEMS[key].stability)
            self.assertEqual(state['bonus'], (0, 0, 0, 0, 0))
            with self.assertRaises(CharacterError):
                self.characters.buy(1, 1, key)

    def test_starter_club_once_and_removal_requires_supply_claim(self):
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['equipped']['武器'], 'starter:club')
        self.assertEqual(state['combat']['攻擊'], 36)
        self.assertEqual(state['combat']['防禦'], 36)
        self.assertEqual(state['stability'], (80, 120))
        self.characters.unequip(1, 1, '武器')
        reloaded = Characters(self.store, RPGSettings())
        self.assertNotIn('武器', reloaded.snapshot(1, 1)['equipped'])
        self.assertEqual(reloaded.inventory_counts(1, 1)['starter:club'], 1)
        self.level(10)
        self.characters.change_job(1, 1, '騎士')
        with self.store.db:
            self.store.db.execute("DELETE FROM rpg_inventory WHERE item_id='starter:club'")
        self.assertEqual(reloaded.snapshot(1, 1)['equipped']['武器'], '騎士:0:武器')
        self.assertNotIn('starter:club', reloaded.inventory_counts(1, 1))
        self.assertEqual(reloaded.claim(1, 1), ['starter:club'])
        self.assertEqual(reloaded.inventory_counts(1, 1)['starter:club'], 1)

    def test_weapons_and_suits_only_grant_direct_stats_and_stability(self):
        self.level(10)
        for job in JOBS:
            equipped = self.characters.change_job(1, 1, job)
            self.assertEqual(equipped['bonus'], (0, 0, 0, 0, 0))
            self.characters.unequip(1, 1, '武器')
            self.characters.unequip(1, 1, '套裝')
            bare = self.characters.snapshot(1, 1)
            self.assertEqual(equipped['total'], bare['total'])
            for stat, bonus in equipped['combat_bonus'].items():
                self.assertEqual(equipped['combat'][stat] - bare['combat'][stat], bonus)
            self.assertEqual(bare['stability'], (100, 100))
            self.assertEqual(equipped['stability'], ITEMS[f'{job}:0:武器'].stability)
        self.assertTrue(all(not any(item.stats) for item in ITEMS.values() if item.slot in ('武器', '套裝')))

    def test_shop_sets_are_twenty_percent_at_each_canonical_tier(self):
        for stage, level in enumerate((10, 20, 50, 90)):
            for job in JOBS:
                growth = GROWTH[job]
                base_stats = tuple(10 + min(level - 1, 9) * 2 + max(0, level - 10) * weight
                                   + stage * weight * 2 for weight in growth)
                naked = combat_from_stats(base_stats)
                weapon = ITEMS[f'{job}:{stage}:武器'].combat
                suit = ITEMS[f'{job}:{stage}:套裝'].combat
                relevant = range(4) if job == '僧侶' else range(3)
                for index in relevant:
                    stat = ('HP', '攻擊', '防禦', '治療量')[index]
                    self.assertEqual(weapon[index] + suit[index], (naked[stat] * 20 + 50) // 100,
                                     (stage, job, stat))

    def test_t20_pure_raid_weapon_and_suit_budget_is_thirty_percent(self):
        raid_items = {
            '裝甲步兵': ('golem:hammer', 'tree:infantry'),
            '騎士': ('golem:sword_shield', 'tree:knight'),
            '弓兵': ('golem:bow', 'tree:archer'),
            '僧侶': ('golem:staff', 'tree:monk'),
        }
        for job, keys in raid_items.items():
            growth = GROWTH[job]
            base_stats = tuple(10 + 18 + 10 * weight + 2 * weight for weight in growth)
            naked = combat_from_stats(base_stats)
            relevant = range(4) if job == '僧侶' else range(3)
            for index in relevant:
                stat = ('HP', '攻擊', '防禦', '治療量')[index]
                total = sum(ITEMS[key].combat[index] for key in keys)
                self.assertEqual(total, (naked[stat] * 30 + 50) // 100, (job, stat))

    def test_shop_payment_gates_repeat_and_rollback(self):
        self.level(90)
        self.characters.change_job(1, 1, '騎士')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,2000)')
        self.characters.buy(1, 1, '騎士:1:武器')
        self.assertEqual(self.store.gold(1, 1), 1500)
        for key in ('騎士:1:武器', '騎士:3:武器', '弓兵:1:武器', 'accessory:0'):
            with self.assertRaises(CharacterError):
                self.characters.buy(1, 1, key)
        self.assertEqual(self.store.gold(1, 1), 1500)
        with self.assertRaises(CharacterError):
            self.characters.buy(2, 1, '騎士:1:套裝')
        self.store.db.execute("CREATE TEMP TRIGGER reject_purchase BEFORE INSERT ON rpg_inventory BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.buy(1, 1, '騎士:1:套裝')
        self.assertEqual(self.store.gold(1, 1), 1500)
        self.store.db.execute('DROP TRIGGER reject_purchase')
        self.level(10)
        with self.assertRaises(CharacterError):
            self.characters.buy(1, 1, '騎士:1:套裝')

    def test_faith_grants_attack_and_healing_not_defense(self):
        self.level(10)
        self.characters.change_job(1, 1, '僧侶')
        before = self.characters.snapshot(1, 1)
        self.characters.equip(1, 1, 'accessory:4')
        after = self.characters.snapshot(1, 1)
        self.assertEqual(after['combat']['攻擊'] - before['combat']['攻擊'], 3)
        self.assertEqual(after['combat']['治療量'] - before['combat']['治療量'], 9)
        self.assertEqual(after['combat']['防禦'], before['combat']['防禦'])
        self.assertEqual(after['combat']['防禦'], after['total'][2] * 3 + after['combat_bonus']['防禦'])
        self.assertEqual(after['combat']['攻擊'], after['total'][1] * 2 + after['total'][4] + after['combat_bonus']['攻擊'])
        self.assertFalse({'物攻', '物防', '法攻', '法防'} & set(after['combat']))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rpg.db'
        self.store = RPGStore(self.path)
        self.addCleanup(self.store.close)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)

    def level(self, level, guild=1, user=1):
        self.store.award_voice([(guild, user, level_floor(level) - self.store.xp(guild, user))])

    def test_militia_and_exact_level_ten_gate(self):
        self.assertEqual(self.characters.snapshot(1, 1)['title'], '民兵')
        self.assertEqual(self.characters.snapshot(1, 1)['total'], (10, 10, 10, 10, 10))
        self.level(9)
        with self.assertRaises(CharacterError):
            self.characters.change_job(1, 1, '騎士')
        self.assertEqual(self.characters.inventory(1, 1), ['starter:club'])
        self.level(10)
        self.assertEqual(self.characters.snapshot(1, 1)['title'], '民兵')
        state = self.characters.change_job(1, 1, '騎士')
        self.assertEqual(state['title'], '早期騎士')
        self.assertEqual(state['capacity'], 2)
        self.assertEqual(len(state['equipped']), 2)
        self.assertEqual(self.store.xp(1, 1), 1154)

    def test_free_equipment_sells_for_zero_cannot_transfer_and_can_be_reclaimed(self):
        self.characters.snapshot(1, 1)
        self.assertTrue(item_sellable(ITEMS['starter:club']))
        self.assertFalse(ITEMS['starter:club'].transferable)
        with self.assertRaises(CharacterError):
            self.characters.dispose(1, 1, 'starter:club', 1, 2)
        self.characters.unequip(1, 1, '武器')
        self.assertEqual(self.characters.dispose(1, 1, 'starter:club', 1), 0)
        self.assertNotIn('武器', self.characters.snapshot(1, 1)['equipped'])
        self.assertEqual(self.characters.claim(1, 1), ['starter:club'])

        self.level(10)
        self.characters.change_job(1, 1, '騎士')
        for key in ('騎士:0:武器', '騎士:0:套裝', 'accessory:0'):
            self.assertTrue(item_sellable(ITEMS[key]))
            self.assertEqual(ITEMS[key].sell_price, 0)
            self.assertFalse(ITEMS[key].transferable)
        with self.assertRaises(CharacterError):
            self.characters.dispose(1, 1, 'accessory:0', 1, 2)
        self.characters.unequip(1, 1, '武器')
        self.assertEqual(self.characters.dispose(1, 1, '騎士:0:武器', 1), 0)
        self.assertEqual(self.characters.dispose(1, 1, 'accessory:0', 1), 0)
        claimed = self.characters.claim(1, 1)
        self.assertIn('騎士:0:武器', claimed)
        self.assertIn('accessory:0', claimed)
        self.characters.dispose(1, 1, '騎士:0:武器', 1)
        self.characters.dispose(1, 1, 'accessory:0', 1)
        self.characters.change_job(1, 1, '弓兵')
        self.assertIn('accessory:0', self.characters.inventory(1, 1))
        self.characters.change_job(1, 1, '騎士')
        self.assertIn('騎士:0:武器', self.characters.inventory(1, 1))

    def test_stage_boundaries_slots_and_claims_are_idempotent(self):
        self.level(10)
        self.characters.change_job(1, 1, '弓兵')
        self.assertEqual(len(self.characters.inventory(1, 1)), 8)
        self.assertEqual(self.characters.claim(1, 1), [])
        for level, name, slots in ((19, '早期弓兵', 2), (20, '弓兵', 3),
                                   (49, '弓兵', 3), (50, '老練弓兵', 4),
                                   (89, '老練弓兵', 4), (90, '精銳弓兵', 5)):
            self.level(level)
            state = self.characters.snapshot(1, 1)
            self.assertEqual((state['title'], state['capacity']), (name, slots))
            self.characters.claim(1, 1)
            self.assertEqual(self.characters.claim(1, 1), [])
        self.assertEqual(len(self.characters.inventory(1, 1)), 8)

    def test_equipment_validation_and_unique_accessory(self):
        self.level(10)
        self.characters.change_job(1, 1, '裝甲步兵')
        before = self.characters.snapshot(1, 1)
        for item, slot in (('unknown', 1), ('accessory:0', 3)):
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, item, slot)
        self.assertEqual(self.characters.snapshot(1, 1), before)
        self.characters.equip(1, 1, 'accessory:0')
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['total'][0], before['total'][0] + 3)
        self.level(20)
        self.characters.equip(1, 1, 'accessory:0', 2)
        state = self.characters.snapshot(1, 1)
        self.assertNotIn('飾品1', state['equipped'])
        self.assertEqual(state['equipped']['飾品2'], 'accessory:0')
        self.characters.equip(1, 1, 'accessory:1', 2)
        self.assertIn('accessory:0', self.characters.inventory(1, 1))
        self.characters.unequip(1, 1, '飾品2')
        with self.assertRaises(CharacterError):
            self.characters.unequip(1, 1, '飾品2')

    def test_change_job_preserves_inventory_and_cannot_farm(self):
        self.level(50)
        old = self.characters.change_job(1, 1, '騎士')
        old_inventory = set(self.characters.inventory(1, 1))
        self.characters.equip(1, 1, 'accessory:0', 3)
        new = self.characters.change_job(1, 1, '裝甲步兵')
        self.assertGreater(old['base'][0], new['base'][0])
        self.assertGreater(old['base'][2], new['base'][2])
        self.assertGreater(new['base'][1], old['base'][1])
        self.assertTrue(old_inventory <= set(self.characters.inventory(1, 1)))
        self.assertNotIn('飾品3', new['equipped'])
        with self.assertRaises(CharacterError):
            self.characters.equip(1, 1, '騎士:2:武器')
        count = len(self.characters.inventory(1, 1))
        self.characters.change_job(1, 1, '騎士')
        self.characters.change_job(1, 1, '裝甲步兵')
        self.assertEqual(len(self.characters.inventory(1, 1)), count)

    def test_reconfigured_requirements_disable_locked_equipment(self):
        self.level(20)
        self.characters.change_job(1, 1, '僧侶')
        self.characters.equip(1, 1, 'accessory:0', 3)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,1000)')
        for slot in ('武器', '套裝'):
            key = f'僧侶:1:{slot}'
            self.characters.buy(1, 1, key)
            self.characters.equip(1, 1, key)
        revised = Characters(self.store, replace(self.settings, regular_level=30))
        self.assertEqual(revised.snapshot(1, 1)['equipped'], {})
        with self.assertRaises(CharacterError):
            revised.equip(1, 1, '僧侶:1:武器')
        revised.equip(1, 1, '僧侶:0:武器')
        revised.unequip(1, 1, '飾品3')

    def test_restart_and_guild_user_isolation(self):
        self.level(10)
        self.characters.change_job(1, 1, '僧侶')
        self.characters.equip(1, 1, 'accessory:4')
        with self.assertRaises(CharacterError):
            self.characters.equip(2, 1, 'accessory:4')
        self.assertEqual(self.characters.job(2, 1), '民兵')
        self.assertEqual(self.characters.job(1, 2), '民兵')
        other = RPGStore(self.path)
        try:
            reloaded = Characters(other, self.settings)
            self.assertEqual(reloaded.snapshot(1, 1), self.characters.snapshot(1, 1))
        finally:
            other.close()

    def test_transaction_rolls_back_partial_job_change(self):
        from unittest.mock import patch
        self.level(10)
        with patch.object(self.characters, '_grant', side_effect=sqlite3.OperationalError('test')):
            with self.assertRaises(sqlite3.OperationalError):
                self.characters.change_job(1, 1, '騎士')
        self.assertEqual(self.characters.job(1, 1), '民兵')

    def test_legacy_xp_and_cooldown_survive_schema_creation(self):
        self.store.award_text(1, 1, 1000, 15, 60)
        Characters(self.store, self.settings)
        self.store.award_text(1, 1, 1050, 15, 60)
        self.assertEqual(self.store.xp(1, 1), 15)
        self.assertEqual(self.characters.job(1, 1), '民兵')

    def test_balanced_growth_and_settings_order(self):
        self.level(90)
        totals = []
        for job in JOBS:
            state = self.characters.change_job(1, 1, job)
            totals.append(sum(state['base']))
        self.assertEqual(len(set(totals)), 1)
        with self.assertRaises(SettingsError):
            replace(self.settings, regular_level=60)
