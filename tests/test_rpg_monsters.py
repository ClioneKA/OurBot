from dataclasses import asdict
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.rpg import RPGStore
from core.rpg_battle import Rule, raid_battle, dump_battle, load_battle
from core.rpg_monsters import prepare_monster, monster_name
from core.rpg_raid_store import RaidStore
from core.rpg_raids import RaidService
from core.settings import RaidSettings


def monster(kind='巨獸', quality='普通'):
    with patch('core.rpg_monsters.random.choices', return_value=[quality]) as draw:
        result = prepare_monster(dict(kind=kind, name=kind, description='測試怪物'))
        draw.assert_called_once_with(('普通', '精英', '首領', '傳說'), weights=[70, 20, 8, 2], k=1)
        return result


def participant():
    return dict(name='玩家', state=dict(level=20, job='弓兵', total=[20] * 5,
        equipped={'武器': 'bow'}, combat={'HP': 10000, '攻擊': 100, '防禦': 20,
        '治療量': 0, '命中率': 99, '閃避率': 0, '暴擊率': 0}), rules=[])


class MonsterTests(unittest.TestCase):
    def test_tiers_and_distinct_stats(self):
        expected = {
            '巨獸': (1, 1065, 170, 40, 36, 88, 0, 10),
            '毒蛛': (1, 568, 120, 35, 72, 95, 15, 15),
            '史萊姆群': (1, 852, 113, 25, 57, 90, 8, 5),
            '鐵殼魔像': (2, 1278, 187, 130, 26, 90, 0, 5),
            '荊棘妖樹': (2, 1491, 127, 84, 31, 92, 0, 5),
        }
        for kind, values in expected.items():
            with self.subTest(kind=kind):
                m = monster(kind)
                enemies = raid_battle([participant()], m, 1).living(1)
                f = enemies[0]
                actual = (m['tier'], sum(e.stats['HP'] for e in enemies), f.stats['攻擊'],
                          f.stats['防禦'], f.dexterity, f.stats['命中率'], f.stats['閃避率'], f.stats['暴擊率'])
                self.assertEqual(actual, values)
                self.assertEqual(monster_name(m), kind)
        self.assertEqual(monster_name(monster('鐵殼魔像', '精英')), '精英・鐵殼魔像')
        for quality, hp, attack, defense in [('普通', 1065, 170, 40), ('精英', 1597, 204, 48),
                                             ('首領', 3195, 255, 60), ('傳說', 5325, 340, 80)]:
            f = raid_battle([participant()], monster(quality=quality), 1).living(1)[0]
            self.assertEqual((f.stats['HP'], f.stats['攻擊'], f.stats['防禦']), (hp, attack, defense))
            self.assertEqual((f.dexterity, f.stats['命中率'], f.stats['暴擊率']), (36, 88, 10))

    def test_group_area_targeting_deaths_and_restart(self):
        battle = raid_battle([participant()], monster('史萊姆群'), 123)
        player, *slimes = battle.fighters
        self.assertEqual(len(slimes), 3)
        player.rules = [Rule(3, 1, True, 'enemies3', 'lowest')]
        self.assertIsNotNone(battle.select(player))
        with patch.object(battle, 'hit') as hit:
            battle.act(player)
            self.assertEqual([c.args[1] for c in hit.call_args_list], slimes)
        player.effects['taunt'] = 100
        with patch.object(battle, 'hit') as hit:
            for slime in slimes:
                battle.act(slime)
            self.assertEqual(len(hit.call_args_list), 3)
            self.assertTrue(all(c.args[1:] == (player, 0.45) for c in hit.call_args_list))
        slimes[0].hp = 0
        player.ready.clear()
        self.assertIsNone(battle.select(player))
        self.assertFalse(battle.check_end())
        with patch.object(battle, 'act') as act:
            battle.step()
            self.assertNotIn(slimes[0], [c.args[0] for c in act.call_args_list])
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        while not battle.result:
            battle.step()
            restored.step()
        self.assertEqual(dump_battle(battle), dump_battle(restored))

    def test_quality_rewards_snapshot_overrides_and_display(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RPGStore(Path(directory) / 'rpg.db')
            try:
                repo = RaidStore(store)
                for channel, (quality, xp, gold, drop) in enumerate([
                    ('普通', 300, 100, .125), ('精英', 450, 150, .2),
                    ('首領', 900, 300, .3), ('傳說', 1500, 500, .5)], 1):
                    m = monster(quality=quality)
                    raid = repo.create(1, channel, m, 0, asdict(RaidSettings()))
                    policy = repo.get(raid['id'])['reward_policy']
                    self.assertEqual((policy['victory_xp'], policy['victory_gold'], policy['drop_chance']), (xp, gold, drop))
                    self.assertEqual(policy['defeat_xp'], 30)
                    embed = RaidService.lobby_embed(type('Service', (), {'settings': RaidSettings()})(), raid)
                    self.assertIn(f'{drop * 100:g}%', embed.fields[-1].value)
                raid = repo.create(1, 10, monster(quality='傳說'), 0, asdict(RaidSettings()),
                                   dict(victory_xp=17, victory_gold=9, drop_chance=.07))
                self.assertEqual(raid['reward_policy']['victory_xp'], 17)
                self.assertEqual(raid['reward_policy']['drop_chance'], .07)
                slime = repo.create(1, 11, monster('史萊姆群', '精英'), 0, asdict(RaidSettings()))
                self.assertEqual(slime['reward_policy']['victory_xp'], 900)
                self.assertEqual(slime['reward_policy']['drop_chance'], 0)
                battle = raid_battle([participant()], slime['monster'], 1)
                embed = RaidService.battle_embed(None, slime, battle)
                self.assertEqual(len(embed.fields[0].value.splitlines()), 3)
                self.assertEqual(embed.fields[1].value, '玩家：10000/10000')
                with store.db:
                    store.db.execute('INSERT INTO rpg_raid_difficulty VALUES (1, 12, 1.1)')
                raid = repo.create(1, 12, monster(quality='精英'), 0, asdict(RaidSettings()))
                self.assertEqual(raid['reward_policy']['victory_xp'], 495)
                self.assertEqual(raid['reward_policy']['victory_gold'], 165)
                self.assertEqual(raid['reward_policy']['drop_chance'], .2)
            finally:
                store.close()
