from pathlib import Path
import json
import tempfile
import unittest

from core.rpg import RPGStore, level_floor
from core.rpg_battle import Tactics, rule_skill
from core.rpg_character import CharacterError, Characters
from core.rpg_loadouts import Loadouts
from core.settings import RPGSettings


class LoadoutStorageTests(unittest.TestCase):
    def test_legacy_basic_target_default_and_invalid_target_are_atomic(self):
        self.characters.change_job(1, 1, '騎士')
        profile = self.loadouts.save(1, 1, 1)
        data = profile['data']
        data.pop('basic_target')
        with self.store.db:
            self.store.db.execute('UPDATE rpg_loadouts SET data=?', (json.dumps(data),))
        self.tactics.configure_basic_target(1, 1, '騎士', 'boss')
        self.loadouts.apply(1, 1, 1)
        self.assertEqual(self.tactics.basic_target(1, 1, '騎士'), 'lowest')
        data['basic_target'] = 'self'
        with self.store.db:
            self.store.db.execute('UPDATE rpg_loadouts SET data=?', (json.dumps(data),))
        self.characters.change_job(1, 1, '弓兵')
        with self.assertRaises(CharacterError):
            self.loadouts.apply(1, 1, 1)
        self.assertEqual(self.characters.job(1, 1), '弓兵')
        self.assertEqual(self.tactics.basic_target(1, 1, '騎士'), 'lowest')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.settings = RPGSettings()
        self.characters = Characters(self.store, self.settings)
        self.tactics = Tactics(self.store)
        self.loadouts = Loadouts(self.store, self.characters, self.tactics)
        self.store.award_voice([(1, 1, level_floor(50))])

    def test_save_and_atomically_apply_job_equipment_rules_and_passive(self):
        self.characters.change_job(1, 1, '裝甲步兵')
        weapon_id = self.characters.grant_item(1, 1, 'plague:axe')[0]
        charm_id = self.characters.grant_item(1, 1, 'puppet:twin_charm')[0]
        self.characters.equip(1, 1, weapon_id)
        self.characters.equip(1, 1, charm_id, 2)
        self.tactics.equip(1, 1, '裝甲步兵', 1, 5)
        self.tactics.configure(1, 1, '裝甲步兵', 1, 3, False,
                               'enemy_hp_lte', 'mechanic', 45)
        self.tactics.equip_passive(1, 1, '裝甲步兵', 2)
        self.tactics.configure_basic_target(1, 1, '裝甲步兵', 'mechanic')
        saved = self.loadouts.save(1, 1, 1)
        self.assertEqual(saved['data']['basic_target'], 'mechanic')
        self.tactics.configure_basic_target(1, 1, '裝甲步兵', 'lowest')
        self.loadouts.rename(1, 1, 1, '鐘龍破甲')

        self.characters.change_job(1, 1, '騎士')
        self.tactics.equip_passive(1, 1, '騎士', 1)
        result = self.loadouts.apply(1, 1, 1)
        self.assertEqual(self.tactics.basic_target(1, 1, '裝甲步兵'), 'mechanic')

        self.assertEqual(result['job'], '裝甲步兵')
        self.assertEqual(result['equipped_instances']['武器'], weapon_id)
        self.assertEqual(result['equipped_instances']['飾品2'], charm_id)
        rule = next(rule for rule in self.tactics.rules(1, 1, '裝甲步兵') if rule.slot == 1)
        self.assertEqual((rule_skill('裝甲步兵', rule).name, rule.priority, rule.enabled,
                          rule.condition, rule.target, rule.condition_value),
                         ('重裝猛擊', 3, False, 'enemy_hp_lte', 'mechanic', 45))
        self.assertEqual(self.tactics.passive(1, 1, '裝甲步兵').id, 2)
        self.assertEqual((saved['slot'], self.loadouts.get(1, 1, 1)['name']), (1, '鐘龍破甲'))

    def test_missing_instance_rejects_without_partial_changes(self):
        self.characters.change_job(1, 1, '弓兵')
        weapon_id = self.characters.snapshot(1, 1)['equipped_instances']['武器']
        self.loadouts.save(1, 1, 1)
        self.characters.change_job(1, 1, '騎士')
        before = self.characters.snapshot(1, 1)
        with self.store.db:
            self.store.db.execute('DELETE FROM rpg_equipment_instances WHERE instance_id=?', (weapon_id,))

        with self.assertRaisesRegex(CharacterError, '已出售或送出'):
            self.loadouts.apply(1, 1, 1)

        after = self.characters.snapshot(1, 1)
        self.assertEqual(after['job'], before['job'])
        self.assertEqual(after['equipped_instances'], before['equipped_instances'])

    def test_three_free_slots_rename_clear_and_no_provisions(self):
        self.characters.change_job(1, 1, '僧侶')
        profiles = self.loadouts.all(1, 1)
        self.assertEqual([profile['name'] for profile in profiles], ['配置 1', '配置 2', '配置 3'])
        with self.assertRaises(CharacterError):
            self.loadouts.get(1, 1, 4)
        self.loadouts.rename(1, 1, 3, '  團隊   治療  ')
        self.assertIsNone(self.loadouts.get(1, 1, 3)['data'])
        self.loadouts.save(1, 1, 3)
        profile = self.loadouts.get(1, 1, 3)
        self.assertEqual(profile['name'], '團隊 治療')
        self.assertNotIn('provisions', profile['data'])
        self.loadouts.clear(1, 1, 3)
        self.assertIsNone(self.loadouts.get(1, 1, 3)['data'])
