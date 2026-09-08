from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_character import (Characters, CharacterError, DYE_PRICE, EMBROIDERY_PRICE,
                                GROWTH, JOBS, ITEMS,
                                combat_from_stats, item_sell_price, item_sellable)
from core.settings import RPGSettings, SettingsError


class CharacterTests(unittest.TestCase):
    def test_each_job_uses_its_distinctive_attack_formula(self):
        stats = (40, 30, 20, 16, 12)
        self.assertEqual(combat_from_stats(stats, '裝甲步兵')['攻擊'], 90)
        self.assertEqual(combat_from_stats(stats, '弓兵')['攻擊'], 54)
        self.assertEqual(combat_from_stats(stats, '騎士')['攻擊'], 80)
        self.assertEqual(combat_from_stats(stats, '僧侶')['攻擊'], 45)
        self.assertEqual(combat_from_stats(stats, '民兵')['攻擊'], 60)

    def test_noah_paint_set_and_socket_variants(self):
        for key in ('paint:red', 'paint:yellow', 'paint:blue', 'paint:set', 'noah:unfinished'):
            self.assertEqual((ITEMS[key].slot, ITEMS[key].category), ('', '製作材料'))
        self.level(45)
        self.characters.change_job(1, 1, '弓兵')
        for key in ('paint:red', 'paint:yellow', 'paint:blue', 'noah:archer:weapon', 'clock:archer'):
            self.characters.grant_item(1, 1, key)
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,5000)')
        self.characters.combine_paint_set(1, 1)
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual(counts['paint:set'], 1)
        self.assertFalse(any(counts.get(key, 0) for key in ('paint:red', 'paint:yellow', 'paint:blue')))

        self.characters.grant_item(1, 1, 'paint:blue')
        self.characters.equip(1, 1, 'noah:archer:weapon')
        colored = self.characters.dye_equipment(1, 1, 'noah:archer:weapon', 'blue')
        self.assertEqual(colored, 'noah:archer:weapon:blue')
        self.assertEqual(self.characters.snapshot(1, 1)['equipped']['武器'], colored)
        self.characters.equip(1, 1, 'clock:archer')
        self.assertEqual(self.characters.snapshot(1, 1)['damage_guard_chance'], 10)

        self.characters.grant_item(1, 1, 'paint:yellow')
        recolored = self.characters.dye_equipment(1, 1, colored, 'yellow')
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['equipped']['武器'], recolored)
        self.assertEqual(ITEMS[recolored].accuracy, 50)
        self.assertNotIn(colored, self.characters.inventory(1, 1))
        self.assertEqual(ITEMS['noah:archer:weapon:red'].combat[1],
                         ITEMS['noah:archer:weapon'].combat[1] * 130 // 100)
        self.assertEqual(ITEMS['noah:archer:suit:red'].combat[0],
                         ITEMS['noah:archer:suit'].combat[0] * 150 // 100)
        self.assertEqual(ITEMS['noah:archer:suit:yellow'].speed, 15)
        self.assertEqual(ITEMS['noah:archer:suit:blue'].evasion, 5)
        self.assertFalse(item_sellable(ITEMS['noah:unfinished']))
        self.assertEqual(self.store.gold(1, 1), 5000 - DYE_PRICE * 2)

    def test_duplicate_equipment_has_independent_socket_and_affixes(self):
        self.level(45)
        self.characters.change_job(1, 1, '弓兵')
        instance_ids = self.characters.grant_item(1, 1, 'noah:archer:weapon', 2)
        self.characters.grant_item(1, 1, 'paint:blue')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,5000)')
        first, second = (f'instance:{instance_id}' for instance_id in instance_ids)

        self.assertEqual(self.characters.dye_equipment(1, 1, first, 'blue'), first)
        counts = self.characters.inventory_counts(1, 1)
        self.assertEqual(counts['noah:archer:weapon'], 1)
        self.assertEqual(counts['noah:archer:weapon:blue'], 1)
        self.assertEqual(self.characters.get_instance(1, 1, second).sockets, ())

        self.characters.set_affixes(1, 1, first, (
            ('mighty', 'combat:1', 7),
            ('swift', 'speed', 3),
        ))
        self.characters.equip(1, 1, first)
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['equipped_instances']['武器'], instance_ids[0])
        self.assertEqual(state['combat_bonus']['攻擊'],
                         ITEMS['noah:archer:weapon:blue'].combat[1]
                         + ITEMS['弓兵:0:套裝'].combat[1] + 7)
        self.assertEqual(state['speed'], 63)
        self.assertEqual(self.characters.resolved_item(
            self.characters.get_instance(1, 1, second)).combat,
            ITEMS['noah:archer:weapon'].combat)

    def test_only_raid_accessories_have_embroidery_slots_and_can_be_restitched(self):
        self.level(30)
        self.characters.change_job(1, 1, '弓兵')
        with self.store.db:
            self.store.db.execute('INSERT INTO rpg_wallets VALUES (1,1,2000)')
        self.assertTrue(all(ITEMS[f'accessory:{index}'].embroidery_slots == 0 for index in range(5)))
        for key in ('raid:0', 'goblin:badge', 'fox:pendant', 'puppet:twin_charm'):
            self.assertEqual(ITEMS[key].embroidery_slots, 1)
        with self.assertRaises(CharacterError):
            self.characters.embroider_accessory(1, 1, 'accessory:0', 'heart')
        instance_id = self.characters.grant_item(1, 1, 'raid:0')[0]
        token = f'instance:{instance_id}'
        self.assertEqual(self.characters.embroider_accessory(1, 1, token, 'heart'), token)
        embroidered = self.characters.resolved_item(self.characters.get_instance(1, 1, token))
        self.assertEqual(embroidered.stats[0], ITEMS['raid:0'].stats[0] + 2)
        with self.assertRaises(CharacterError):
            self.characters.embroider_accessory(1, 1, token, 'heart')
        self.characters.embroider_accessory(1, 1, token, 'wing')
        restitched = self.characters.resolved_item(self.characters.get_instance(1, 1, token))
        self.assertEqual(restitched.stats[0], ITEMS['raid:0'].stats[0])
        self.assertEqual(restitched.stats[3], ITEMS['raid:0'].stats[3] + 2)
        self.assertEqual(self.store.gold(1, 1), 2000 - EMBROIDERY_PRICE * 2)

    def test_tailor_does_not_consume_or_modify_items_when_gold_is_insufficient(self):
        self.level(45)
        self.characters.change_job(1, 1, '弓兵')
        weapon_id = self.characters.grant_item(1, 1, 'noah:archer:weapon')[0]
        accessory_id = self.characters.grant_item(1, 1, 'raid:0')[0]
        self.characters.grant_item(1, 1, 'paint:red')

        with self.assertRaises(CharacterError):
            self.characters.dye_equipment(1, 1, f'instance:{weapon_id}', 'red')
        with self.assertRaises(CharacterError):
            self.characters.embroider_accessory(1, 1, f'instance:{accessory_id}', 'heart')

        self.assertEqual(self.characters.inventory_counts(1, 1)['paint:red'], 1)
        self.assertEqual(self.characters.get_instance(1, 1, weapon_id).sockets, ())
        self.assertEqual(self.characters.get_instance(1, 1, accessory_id).affixes, ())

    def test_legacy_quantities_and_colored_equipment_migrate_once(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = RPGStore(Path(directory.name) / 'legacy.db')
        self.addCleanup(store.close)
        with store.db:
            store.db.execute('''CREATE TABLE rpg_inventory (
                guild_id INTEGER, user_id INTEGER, item_id TEXT, quantity INTEGER,
                PRIMARY KEY(guild_id,user_id,item_id))''')
            store.db.execute('''CREATE TABLE rpg_equipment (
                guild_id INTEGER, user_id INTEGER, slot TEXT, item_id TEXT,
                PRIMARY KEY(guild_id,user_id,slot))''')
            store.db.execute('''CREATE TABLE rpg_characters (
                guild_id INTEGER, user_id INTEGER, job TEXT,
                PRIMARY KEY(guild_id,user_id))''')
            store.db.execute('''CREATE TABLE rpg_starter_claims (
                guild_id INTEGER, user_id INTEGER,
                PRIMARY KEY(guild_id,user_id))''')
            store.db.execute("INSERT INTO rpg_inventory VALUES (1,2,'noah:archer:weapon:red',2)")
            store.db.execute("INSERT INTO rpg_equipment VALUES (1,2,'武器','noah:archer:weapon:red')")
            store.db.execute("INSERT INTO rpg_characters VALUES (1,2,'弓兵')")
            store.db.execute("INSERT INTO rpg_starter_claims VALUES (1,2)")

        store.award_voice([(1, 2, level_floor(45))])

        migrated = Characters(store, self.settings)
        instances = migrated.equipment_instances(1, 2)
        self.assertEqual(len(instances), 2)
        self.assertTrue(all(instance.item_id == 'noah:archer:weapon' for instance in instances))
        self.assertTrue(all(dict(instance.sockets)[0] == 'paint:red' for instance in instances))
        self.assertEqual(migrated.snapshot(1, 2)['equipped']['武器'], 'noah:archer:weapon:red')
        self.assertIsNone(store.db.execute("SELECT 1 FROM rpg_inventory WHERE item_id LIKE 'noah:%'").fetchone())

        reloaded = Characters(store, self.settings)
        self.assertEqual(len(reloaded.equipment_instances(1, 2)), 2)

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
        self.assertEqual(ITEMS['plague:bow'].speed, 8)
        self.assertLess(ITEMS['plague:bow'].speed, ITEMS['弓兵:2:武器'].speed)
        self.assertEqual(ITEMS['puppet:twin_charm'].stats, (2, 2, 2, 2, 2))
        self.assertIn('額外治療', item_text(ITEMS['puppet:twin_charm']))
        self.assertFalse(item_sellable(ITEMS['paint:red']))
        self.assertTrue(ITEMS['paint:red'].transferable)

    def test_tier_four_raid_items_require_level_forty_and_snapshot_effects(self):
        from core.rpg_character import item_text
        self.level(30)
        self.characters.change_job(1, 1, '弓兵')
        for key in ('twin_beast:archer:weapon', 'twin_beast:archer:suit', 'twin_beast:charm'):
            self.characters.grant_item(1, 1, key)
            with self.assertRaises(CharacterError):
                self.characters.equip(1, 1, key)
        self.level(40)
        self.characters.equip(1, 1, 'twin_beast:archer:weapon')
        self.characters.equip(1, 1, 'twin_beast:archer:suit')
        self.characters.equip(1, 1, 'twin_beast:charm')
        state = self.characters.snapshot(1, 1)
        self.assertEqual(state['alternating_damage_percent'], 10)
        self.assertFalse(state['defense_conversion'])
        self.assertEqual(ITEMS['twin_beast:archer:weapon'].accuracy, 40)
        self.assertIn('奇數回合單體直接攻擊 +10%', item_text(ITEMS['twin_beast:charm']))
        self.assertIn('防禦擋下', item_text(ITEMS['whale:charm']))

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
            self.assertEqual(item.speed, 7)
            self.assertLess(item.speed, ITEMS[f'{item.job}:2:武器'].speed)
            self.assertGreater(item.combat[1], ITEMS[f'{item.job}:1:武器'].combat[1])
        self.assertIn('閃避值 +5', item_text(ITEMS['fox:pendant']))

    def test_accuracy_is_independent_of_dexterity_and_comes_from_weapons(self):
        from core.rpg_character import combat_from_stats
        for dexterity in (10, 120, 375, 500):
            with self.subTest(dexterity=dexterity):
                self.assertEqual(combat_from_stats((10, 10, 10, dexterity, 10))['命中率'], 95)
        self.assertEqual(ITEMS['弓兵:0:武器'].accuracy, 10)
        self.assertEqual(ITEMS['弓兵:1:武器'].accuracy, 20)
        self.assertEqual(ITEMS['plague:bow'].accuracy, 30)
        self.assertEqual(ITEMS['noah:archer:weapon'].accuracy, 45)

    def test_dexterity_critical_curve_and_linear_evasion_hit_level_120_archer_targets(self):
        level_one = combat_from_stats((10, 10, 10, 10, 10), '弓兵')
        level_120 = combat_from_stats((260, 376, 144, 376, 144), '弓兵')
        self.assertEqual(level_one['暴擊率'], 10)
        self.assertEqual(level_one['閃避率'], 0)
        self.assertEqual(level_120['暴擊率'], 95)
        self.assertEqual(level_120['閃避率'], 35)
        self.assertGreater(
            combat_from_stats((10, 10, 10, 60, 10))['暴擊率'] - level_one['暴擊率'],
            level_120['暴擊率'] - combat_from_stats((10, 10, 10, 326, 10))['暴擊率'])

    def test_speed_is_level_independent_and_modified_by_equipment(self):
        from core.rpg_character import item_text
        self.level(10)
        state = self.characters.change_job(1, 1, '弓兵')
        self.assertEqual((state['speed'], state['combat']['速度']), (60, 60))
        self.assertNotIn('速度', combat_from_stats((10, 10, 10, 500, 10)))

        self.level(90)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO rpg_inventory(guild_id,user_id,item_id) VALUES (1,1,'弓兵:3:武器')")
        self.characters.equip(1, 1, '弓兵:3:武器')
        state = self.characters.snapshot(1, 1)
        self.assertEqual((state['speed'], state['combat']['速度']), (75, 75))
        self.assertIn('速度 +15', item_text(ITEMS['弓兵:3:武器']))

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
            self.assertEqual(item.speed, 7)
            self.assertLess(item.speed, ITEMS[f'{item.job}:2:武器'].speed)
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
        self.assertEqual(state['combat']['攻擊'], 24)
        self.assertEqual(state['combat']['防禦'], 36)
        self.assertEqual(state['stability'], (80, 120))
        self.characters.unequip(1, 1, '武器')
        reloaded = Characters(self.store, RPGSettings())
        self.assertNotIn('武器', reloaded.snapshot(1, 1)['equipped'])
        self.assertEqual(reloaded.inventory_counts(1, 1)['starter:club'], 1)
        self.level(10)
        self.characters.change_job(1, 1, '騎士')
        with self.store.db:
            starter = reloaded.get_instance(1, 1, 'starter:club')
            self.store.db.execute('DELETE FROM rpg_equipment_instances WHERE instance_id=?',
                                  (starter.instance_id,))
        self.assertEqual(reloaded.snapshot(1, 1)['equipped']['武器'], '騎士:0:武器')
        self.assertNotIn('starter:club', reloaded.inventory_counts(1, 1))
        self.assertEqual(reloaded.claim(1, 1), ['starter:club'])
        self.assertEqual(reloaded.inventory_counts(1, 1)['starter:club'], 1)

    def test_public_showcase_must_be_owned_and_persists(self):
        self.assertFalse(self.characters.has_character(1, 1))
        self.characters.snapshot(1, 1)
        self.assertTrue(self.characters.has_character(1, 1))
        self.assertIsNone(self.characters.showcase(1, 1))
        with self.assertRaises(CharacterError):
            self.characters.set_showcase(1, 1, 'clock:archer')
        self.characters.grant_item(1, 1, 'paint:red')
        self.characters.set_showcase(1, 1, 'paint:red')
        self.assertEqual(self.characters.showcase(1, 1), 'paint:red')
        reloaded = Characters(self.store, self.settings)
        self.assertEqual(reloaded.showcase(1, 1), 'paint:red')
        self.characters.set_showcase(1, 1, None)
        self.assertIsNone(self.characters.showcase(1, 1))

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
                naked = combat_from_stats(base_stats, job)
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
            naked = combat_from_stats(base_stats, job)
            relevant = range(4) if job == '僧侶' else range(3)
            for index in relevant:
                stat = ('HP', '攻擊', '防禦', '治療量')[index]
                total = sum(ITEMS[key].combat[index] for key in keys)
                self.assertEqual(total, (naked[stat] * 30 + 50) // 100, (job, stat))

    def test_t40_raid_sets_progress_past_t30_and_have_resale_value(self):
        t30 = {
            '裝甲步兵': ('plague:axe', 'clock:infantry'),
            '騎士': ('plague:sword_shield', 'clock:knight'),
            '弓兵': ('plague:bow', 'clock:archer'),
            '僧侶': ('plague:staff', 'clock:monk'),
        }
        t40 = {
            '裝甲步兵': ('twin_beast:infantry:weapon', 'twin_beast:infantry:suit'),
            '騎士': ('whale:knight:weapon', 'whale:knight:suit'),
            '弓兵': ('twin_beast:archer:weapon', 'twin_beast:archer:suit'),
            '僧侶': ('whale:monk:weapon', 'whale:monk:suit'),
        }
        for job in JOBS:
            for older, newer in zip(t30[job], t40[job]):
                self.assertTrue(all(new >= old for new, old in
                                    zip(ITEMS[newer].combat, ITEMS[older].combat)),
                                (job, older, newer))
            self.assertTrue(any(
                sum(ITEMS[key].combat[index] for key in t40[job])
                > sum(ITEMS[key].combat[index] for key in t30[job])
                for index in range(4)))
        for key in (
            'twin_beast:infantry:weapon', 'twin_beast:infantry:suit',
            'twin_beast:archer:weapon', 'twin_beast:archer:suit', 'twin_beast:charm',
            'whale:knight:weapon', 'whale:knight:suit',
            'whale:monk:weapon', 'whale:monk:suit', 'whale:charm',
        ):
            self.assertEqual((ITEMS[key].value, item_sell_price(ITEMS[key])), (1000, 200))

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
        self.store.db.execute("CREATE TEMP TRIGGER reject_purchase BEFORE INSERT ON rpg_equipment_instances BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.characters.buy(1, 1, '騎士:1:套裝')
        self.assertEqual(self.store.gold(1, 1), 1500)
        self.store.db.execute('DROP TRIGGER reject_purchase')
        self.level(10)
        with self.assertRaises(CharacterError):
            self.characters.buy(1, 1, '騎士:1:套裝')

    def test_monk_faith_grants_job_attack_and_healing_but_not_defense(self):
        self.level(10)
        self.characters.change_job(1, 1, '僧侶')
        before = self.characters.snapshot(1, 1)
        self.characters.equip(1, 1, 'accessory:4')
        after = self.characters.snapshot(1, 1)
        self.assertEqual(after['combat']['攻擊'] - before['combat']['攻擊'], 3)
        self.assertEqual(after['combat']['治療量'] - before['combat']['治療量'], 9)
        self.assertEqual(after['combat']['防禦'], before['combat']['防禦'])
        self.assertEqual(after['combat']['防禦'], after['total'][2] * 3 + after['combat_bonus']['防禦'])
        self.assertEqual(after['combat']['攻擊'],
                         after['total'][1] + after['total'][4] * 5 // 4
                         + after['combat_bonus']['攻擊'])
        self.assertFalse({'物攻', '物防', '法攻', '法防'} & set(after['combat']))

    def test_professions_have_distinct_critical_damage(self):
        self.level(10)
        for job, expected in (('裝甲步兵', 150), ('騎士', 125),
                              ('弓兵', 175), ('僧侶', 125)):
            state = self.characters.change_job(1, 1, job)
            self.assertEqual(state['critical_damage_percent'], expected)

    def test_tier_five_and_six_sets_and_cycle_emblem(self):
        from core.rpg_character import item_text
        self.level(50)
        self.characters.change_job(1, 1, '裝甲步兵')
        for key in ('forge:infantry:weapon', 'forge:infantry:suit',
                    'star:infantry:weapon', 'star:infantry:suit', 'cycle:emblem'):
            self.characters.grant_item(1, 1, key)
        self.characters.equip(1, 1, 'forge:infantry:weapon')
        single = self.characters.snapshot(1, 1)
        self.assertEqual((single['active_set'], single['stability']), ('', (60, 140)))
        self.characters.equip(1, 1, 'forge:infantry:suit')
        molten = self.characters.snapshot(1, 1)
        self.assertEqual((molten['active_set'], molten['stability']), ('molten_vein', (75, 140)))
        self.assertIn('熔脈', molten['set_bonus_text'])
        with self.assertRaises(CharacterError):
            self.characters.equip(1, 1, 'star:infantry:weapon')
        with self.assertRaises(CharacterError):
            self.characters.equip(1, 1, 'cycle:emblem')

        self.level(60)
        self.characters.equip(1, 1, 'star:infantry:weapon')
        mixed = self.characters.snapshot(1, 1)
        self.assertEqual((mixed['active_set'], mixed['stability']), ('', (60, 140)))
        self.characters.equip(1, 1, 'star:infantry:suit')
        self.characters.equip(1, 1, 'cycle:emblem')
        star = self.characters.snapshot(1, 1)
        self.assertEqual((star['active_set'], star['stability']), ('starforged', (85, 140)))
        self.assertEqual(star['first_skill_cooldown_reduction'], 1)
        self.assertIn('2 件', item_text(ITEMS['star:infantry:weapon']))
        self.assertIn('冷卻 -1', item_text(ITEMS['cycle:emblem']))

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
