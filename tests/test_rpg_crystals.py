from pathlib import Path
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_character import (COMBAT_NAMES, GROWTH, CharacterError, Characters,
                                ELITE_CRYSTAL_SLOTS, ITEMS, combat_from_stats)
from core.rpg_crystals import (
    COLOR_AFFIXES,
    CRYSTAL_REMOVAL_PRICE,
    CRYSTAL_TYPES,
    OUTLINE_AFFIXES,
    QUALITY_SELL_PRICES,
    SOURCE_AFFIXES,
    CrystalStore,
)
from core.rpg_painted_maze import PaintedMazeStore
from core.settings import RPGSettings


class CrystalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rpg = RPGStore(Path(self.temp.name) / 'rpg.db')
        self.addCleanup(self.rpg.close)
        self.characters = Characters(self.rpg, RPGSettings())
        self.maze = PaintedMazeStore(self.rpg)
        self.crystals = CrystalStore(self.rpg)
        for user_id in (1, 2, 3):
            self.characters.create(1, user_id)
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.maze.create(1, 1, 'painting:balloon', 50, now=100, seed=445)
        self.maze.change_member(room['id'], 2, 50, now=101)
        participants = [
            {'id': 1, 'state': {'level': 60, 'job': '弓兵', 'combat': {'HP': 1000}}},
            {'id': 2, 'state': {'level': 60, 'job': '僧侶', 'combat': {'HP': 1000}}},
        ]
        self.room = self.maze.begin(room['id'], 1, participants, now=110)
        # These inventory/socket tests use legacy immediately released rewards.
        # Escrow and final-risk payouts are exercised in test_rpg_painted_maze_live.
        self.room.pop('reward_policy')
        with self.rpg.db:
            self.maze._save(self.room)

    def finish_stage(self, stage, now):
        self.room = self.maze.record_boss_victory(self.room['id'], 1, now=now)
        now += 1
        return now

    def resolve_vote(self, now):
        deadline = self.room['contract_vote']['deadline']
        self.room = self.maze.resolve_contract(self.room['id'], now=deadline)
        return max(now, deadline + 1)

    def test_each_stage_grants_one_correct_independent_instance_per_member(self):
        now = self.finish_stage(1, 120)
        stage_one = self.crystals.seal_stage(self.room['id'], 1, now=now)
        self.assertEqual([crystal.crystal_type for crystal in stage_one], ['outline', 'outline'])
        self.assertTrue(all(crystal.affix_id in OUTLINE_AFFIXES for crystal in stage_one))
        self.assertEqual(len({crystal.instance_id for crystal in stage_one}), 2)
        self.assertEqual(stage_one[0].source_painting_id, self.room['paintings'][0]['id'])

        now = self.resolve_vote(now)
        now = self.finish_stage(2, now)
        stage_two = self.crystals.seal_stage(self.room['id'], 2, now=now)
        self.assertTrue(all(crystal.crystal_type == 'color' for crystal in stage_two))
        self.assertTrue(all(crystal.affix_id in COLOR_AFFIXES for crystal in stage_two))
        self.assertTrue(all(crystal.job is None for crystal in stage_two))

        now = self.resolve_vote(now)
        now = self.finish_stage(3, now)
        stage_three = self.crystals.seal_stage(self.room['id'], 3, now=now)
        self.assertEqual([crystal.job for crystal in stage_three], ['弓兵', '僧侶'])
        self.assertIn(stage_three[0].affix_id, SOURCE_AFFIXES['弓兵'])
        self.assertIn(stage_three[1].affix_id, SOURCE_AFFIXES['僧侶'])
        self.assertEqual(len(self.crystals.inventory(1, 1)), 3)
        self.assertEqual(len(self.crystals.inventory(1, 2)), 3)
        self.assertEqual(set(CRYSTAL_TYPES), {'outline', 'color', 'source'})

    def test_stage_seal_is_idempotent_and_does_not_reroll(self):
        now = self.finish_stage(1, 120)
        first = self.crystals.seal_stage(self.room['id'], 1, now=now)
        second = self.crystals.seal_stage(self.room['id'], 1, now=999)
        self.assertEqual(first, second)
        self.assertEqual(len(self.crystals.inventory(1, 1)), 1)
        self.assertEqual(len(self.crystals.inventory(1, 2)), 1)
        saved = self.maze.get(self.room['id'])
        markers = [reward for reward in saved['sealed_rewards'] if reward['kind'] == 'crystals']
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['instance_ids'], [crystal.instance_id for crystal in first])

    def test_unfinished_stage_cannot_grant_crystals(self):
        with self.assertRaisesRegex(Exception, '尚未完成'):
            self.crystals.seal_stage(self.room['id'], 1, now=120)
        self.assertEqual(self.crystals.inventory(1, 1), [])

    def test_unembedded_crystal_can_be_transferred_or_sold(self):
        now = self.finish_stage(1, 120)
        first, second = self.crystals.seal_stage(self.room['id'], 1, now=now)
        moved = self.crystals.transfer(1, 1, first.instance_id, 3)
        self.assertEqual(moved.user_id, 3)
        self.assertEqual([item.instance_id for item in self.crystals.inventory(1, 3)],
                         [first.instance_id])
        price = self.crystals.sell(1, 2, second.instance_id)
        self.assertEqual(price, QUALITY_SELL_PRICES[second.quality])
        self.assertEqual(self.rpg.gold(1, 2), price)
        self.assertIsNone(self.crystals.get(second.instance_id))

    def test_t60_elite_names_stats_and_three_typed_slots(self):
        expected = {
            'maze:infantry:weapon': '未竟戰繪・斷彩戰斧',
            'maze:infantry:suit': '未竟戰繪・重彩戰甲',
            'maze:knight:weapon': '未竟守望・界框劍盾',
            'maze:knight:suit': '未竟守望・定框重甲',
            'maze:archer:weapon': '未竟追彩・流彩長弓',
            'maze:archer:suit': '未竟追彩・風描獵裝',
            'maze:monk:weapon': '未竟聖像・調色聖杖',
            'maze:monk:suit': '未竟聖像・祈彩法衣',
        }
        for item_id, name in expected.items():
            item = ITEMS[item_id]
            self.assertEqual((item.name, item.required_level, item.crystal_slots),
                             (name, 60, ELITE_CRYSTAL_SLOTS))
            self.assertFalse(item.set_id)
        combined = {
            job: tuple(a + b for a, b in zip(
                ITEMS[f'maze:{job}:weapon'].combat, ITEMS[f'maze:{job}:suit'].combat))
            for job in ('infantry', 'knight', 'archer', 'monk')
        }
        self.assertEqual(combined, {
            'infantry': (684, 200, 142, 0),
            'knight': (873, 136, 200, 0),
            'archer': (494, 166, 86, 0),
            'monk': (494, 136, 86, 256),
        })
        jobs = dict(infantry='裝甲步兵', knight='騎士', archer='弓兵', monk='僧侶')
        for slug, job in jobs.items():
            base_stats = tuple(10 + 18 + 50 * weight + 4 * weight for weight in GROWTH[job])
            naked = combat_from_stats(base_stats, job)
            equipped = combined[slug]
            for stat, bonus in zip(COMBAT_NAMES, equipped):
                if naked[stat] and bonus:
                    self.assertGreaterEqual(bonus / naked[stat], .345)
                    self.assertLessEqual(bonus / naked[stat], .355)

    def test_equipping_duplicate_unique_source_effect_is_rejected_before_commit(self):
        self.rpg.award_voice([(1, 3, level_floor(60))])
        self.characters.change_job(1, 3, '弓兵')
        weapon_id = self.characters.grant_item(1, 3, 'maze:archer:weapon')[0]
        suit_id = self.characters.grant_item(1, 3, 'maze:archer:suit')[0]
        with self.rpg.db:
            for reward_slot, equipment_id in enumerate((weapon_id, suit_id)):
                self.rpg.db.execute('''INSERT INTO rpg_crystal_instances
                    (guild_id,user_id,crystal_type,quality,affix_id,effect_keys,rolled_values,
                     job,source_painting_id,source_room_id,source_user_id,source_stage,
                     reward_slot,created_at,equipment_instance_id,socket_index)
                    VALUES (1,3,'source','習作','source_focused_shot','["focused_shot"]',
                            '[3]','弓兵','test','duplicate-test',3,3,?,1,?,?)''',
                    (reward_slot, equipment_id, 2))
        self.characters.equip(1, 3, weapon_id)
        with self.assertRaisesRegex(CharacterError, '重複的唯一結晶效果'):
            self.characters.equip(1, 3, suit_id)
        state = self.characters.snapshot(1, 3)
        self.assertEqual(state['equipped_instances']['武器'], weapon_id)
        self.assertNotEqual(state['equipped_instances'].get('套裝'), suit_id)

    def test_socket_type_job_removal_and_outline_stats_are_transactional(self):
        now = self.finish_stage(1, 120)
        outlines = self.crystals.seal_stage(self.room['id'], 1, now=now)
        now = self.resolve_vote(now)
        now = self.finish_stage(2, now)
        colors = self.crystals.seal_stage(self.room['id'], 2, now=now)
        now = self.resolve_vote(now)
        now = self.finish_stage(3, now)
        sources = self.crystals.seal_stage(self.room['id'], 3, now=now)
        self.rpg.award_voice([(1, 1, level_floor(60))])
        self.characters.change_job(1, 1, '弓兵')
        equipment_id = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]

        socketed = self.crystals.socket(1, 1, outlines[0].instance_id, equipment_id)
        self.assertEqual((socketed.equipment_instance_id, socketed.socket_index), (equipment_id, 0))
        resolved = self.characters.resolved_item(
            self.characters.get_instance(1, 1, equipment_id))
        base = ITEMS['maze:archer:weapon']
        for effect, value in zip(outlines[0].effect_keys, outlines[0].rolled_values):
            if effect in ('HP', '攻擊', '防禦', '治療量'):
                index = ('HP', '攻擊', '防禦', '治療量').index(effect)
                self.assertEqual(resolved.combat[index], base.combat[index] + value)

        self.crystals.socket(1, 1, colors[0].instance_id, equipment_id)
        self.crystals.socket(1, 1, sources[0].instance_id, equipment_id)
        self.characters.equip(1, 1, equipment_id)
        snapshot = self.characters.snapshot(1, 1)
        self.assertEqual(len(snapshot['crystal_effects']), 3)
        self.assertEqual({effect['type'] for effect in snapshot['crystal_effects']},
                         {'outline', 'color', 'source'})
        monk_source = self.crystals.transfer(1, 2, sources[1].instance_id, 1)
        suit_id = self.characters.grant_item(1, 1, 'maze:archer:suit')[0]
        with self.assertRaisesRegex(CharacterError, '職業與裝備不符'):
            self.crystals.socket(1, 1, monk_source.instance_id, suit_id)

        with self.rpg.db:
            self.rpg.db.execute('INSERT OR REPLACE INTO rpg_wallets VALUES (1,1,2000)')
        removed = self.crystals.remove(1, 1, equipment_id, 'outline')
        self.assertIsNone(removed.equipment_instance_id)
        self.assertEqual(self.rpg.gold(1, 1), 2000 - CRYSTAL_REMOVAL_PRICE)

    def test_overwrite_destroys_old_crystal_and_equipment_transfer_keeps_new_one(self):
        now = self.finish_stage(1, 120)
        first, second = self.crystals.seal_stage(self.room['id'], 1, now=now)
        second = self.crystals.transfer(1, 2, second.instance_id, 1)
        equipment_id = self.characters.grant_item(1, 1, 'maze:archer:weapon')[0]
        self.crystals.socket(1, 1, first.instance_id, equipment_id)
        with self.assertRaisesRegex(CharacterError, '已有結晶'):
            self.crystals.socket(1, 1, second.instance_id, equipment_id)
        self.crystals.socket(1, 1, second.instance_id, equipment_id, replace_existing=True)
        self.assertIsNone(self.crystals.get(first.instance_id))
        self.assertEqual(self.crystals.get(second.instance_id).equipment_instance_id, equipment_id)

        self.characters.dispose(1, 1, f'instance:{equipment_id}', 1, recipient=3)
        moved = self.crystals.get(second.instance_id)
        self.assertEqual((moved.user_id, moved.equipment_instance_id), (3, equipment_id))
        self.assertEqual(self.characters.get_instance(1, 3, equipment_id).item_id,
                         'maze:archer:weapon')


if __name__ == '__main__':
    unittest.main()
