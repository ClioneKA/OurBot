from dataclasses import asdict
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.rpg import RPGStore
from core.rpg_battle import Rule, raid_battle, dump_battle, load_battle
from core.rpg_character import ITEMS
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
    def test_goblin_group_buffs_death_and_restart(self):
        battle = raid_battle([participant()], monster('哥布林戰團'), 42)
        player, captain, *grunts = battle.fighters
        self.assertEqual([f.job for f in battle.living(1)], ['哥布林隊長', '哥布林打手', '哥布林打手'])
        self.assertEqual(sum(f.hp for f in battle.living(1)), 1252)
        self.assertEqual(captain.stats['攻擊'], 224)
        player.rules = [Rule(3, 1, True, 'enemies3', 'lowest')]
        with patch.object(battle, 'hit') as hit:
            battle.act(player)
            self.assertEqual([c.args[1] for c in hit.call_args_list], [captain, *grunts])
        battle.round = 3
        with patch.object(battle, 'hit') as hit:
            battle.act(captain)
            hit.assert_not_called()
        self.assertTrue(all(f.has('bless', 4) and not f.has('bless', 5) for f in [captain, *grunts]))
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        battle.step()
        restored.step()
        self.assertEqual(dump_battle(battle), dump_battle(restored))
        captain.hp = 0
        battle.round = 5
        with patch.object(battle, 'act') as act:
            battle.step()
            self.assertNotIn(captain, [c.args[0] for c in act.call_args_list])
        self.assertFalse(any(f.has('bless', 6) for f in grunts))

    def test_badge_party_scaling_is_personal_capped_and_frozen(self):
        for count in (1, 4, 10, 12):
            people = [participant() for _ in range(count)]
            people[0]['state']['equipped']['飾品1'] = 'goblin:badge'
            battle = raid_battle(people, monster(), 10)
            wearer = battle.fighters[0]
            counted = min(5, (count + 1) // 2)
            original = dict(people[0]['state']['combat'])
            for stat, per_player in (('HP', 10), ('攻擊', 3), ('防禦', 3), ('治療量', 3)):
                self.assertEqual(wearer.stats[stat], original[stat] + counted * per_player)
            self.assertEqual(wearer.speed, 60)
            for stat, divisor in (('命中率', 5), ('閃避率', 10), ('暴擊率', 8)):
                self.assertEqual(wearer.stats[stat], original[stat] + (20 + counted) // divisor - 20 // divisor)
            self.assertEqual(wearer.hp, wearer.stats['HP'])
            self.assertEqual(people[0]['state']['combat'], original)
            if count > 1:
                self.assertEqual(battle.fighters[1].stats, original)
                battle.fighters[1].hp = 0
            stats = dict(wearer.stats)
            restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
            self.assertEqual(restored.fighters[0].stats, stats)
            battle.step()
            restored.step()
            self.assertEqual(wearer.stats, stats)
            self.assertEqual(dump_battle(battle), dump_battle(restored))

    def test_badge_respects_rate_caps_and_preserves_equipment_bonuses(self):
        from core.rpg_character import combat_from_stats
        people = [participant() for _ in range(10)]
        state = people[0]['state']
        state['equipped']['飾品1'] = 'goblin:badge'
        state['total'] = [50, 60, 70, 374, 80]
        state['combat'] = combat_from_stats(state['total'])
        state['combat']['攻擊'] += ITEMS['goblin:bow'].combat[1]
        wearer = raid_battle(people, monster(), 10).fighters[0]
        self.assertEqual(wearer.stats, {'HP': 600, '攻擊': 274, '防禦': 225,
                                     '治療量': 255, '命中率': 150, '閃避率': 35, '暴擊率': 50})
        self.assertEqual(wearer.speed, 60)

    def test_tiers_and_distinct_stats(self):
        expected = {
            '月影妖狐': (2, 1152, 266, 45, 70, 95, 20, 15),
            '血翼蝠王': (2, 1166, 277, 52, 65, 94, 12, 10),
            '巨獸': (1, 783, 151, 24, 40, 88, 0, 10),
            '毒蛛': (1, 594, 131, 21, 65, 95, 15, 15),
            '史萊姆群': (0, 852, 113, 25, 55, 90, 8, 5),
            '鐵殼魔像': (2, 1296, 288, 130, 35, 90, 0, 5),
            '荊棘妖樹': (2, 1310, 449, 84, 40, 92, 0, 5),
            '哥布林戰團': (2, 1252, 224, 52, 55, 92, 8, 10),
            '深淵鐘龍': (3, 2604, 361, 92, 45, 92, 0, 10),
            '王城傀儡師': (3, 1772, 470, 72, 55, 94, 8, 10),
            '瘟疫縫合獸': (3, 2357, 613, 80, 50, 93, 3, 8),
        }
        for kind, values in expected.items():
            with self.subTest(kind=kind):
                m = monster(kind)
                enemies = raid_battle([participant()], m, 1).living(1)
                f = enemies[0]
                actual = (m['tier'], sum(e.stats['HP'] for e in enemies), f.stats['攻擊'],
                          f.stats['防禦'], f.speed, f.stats['命中率'], f.stats['閃避率'], f.stats['暴擊率'])
                self.assertEqual(actual, values)
                self.assertEqual(monster_name(m), kind)
        self.assertEqual(monster_name(monster('鐵殼魔像', '精英')), '精英・鐵殼魔像')
        for quality, hp, attack, defense in [
                ('普通', 783, 151, 24), ('精英', 1039, 206, 32),
                ('首領', 1296, 262, 40), ('傳說', 1809, 391, 56)]:
            f = raid_battle([participant()], monster(quality=quality), 1).living(1)[0]
            self.assertEqual((f.stats['HP'], f.stats['攻擊'], f.stats['防禦']), (hp, attack, defense))
            self.assertEqual((f.speed, f.stats['命中率'], f.stats['暴擊率']), (40, 88, 10))

    def test_v3_monster_stats_use_fixed_tier_and_quality_levels(self):
        attacks, hit_points = [], []
        for level in (10, 20, 50, 90):
            player = participant()
            player['state']['level'] = level
            enemy = raid_battle([player], monster('巨獸'), 1).living(1)[0]
            attacks.append(enemy.stats['攻擊'])
            hit_points.append(enemy.stats['HP'])
        self.assertEqual(attacks, [151, 151, 151, 151])
        self.assertEqual(hit_points, [783, 783, 783, 783])

        player = participant()
        player['state']['level'] = 30
        golem = raid_battle([player], monster('鐵殼魔像'), 1).living(1)[0]
        self.assertEqual((golem.stats['HP'], golem.stats['攻擊'], golem.stats['防禦']),
                         (1296, 288, 130))

        clock_dragon = raid_battle([player], monster('深淵鐘龍'), 1).living(1)[0]
        self.assertEqual((clock_dragon.stats['HP'], clock_dragon.stats['攻擊'],
                          clock_dragon.stats['防禦']), (2604, 361, 92))

    def test_old_announcements_keep_multiplier_speed(self):
        old = monster('巨獸')
        old['balance_version'] = 2
        old['profile']['speed'] = 0.7
        enemy = raid_battle([participant()], old, 1).living(1)[0]
        self.assertEqual(enemy.speed, 22)

    def test_v2_announcements_keep_legacy_player_and_monster_speed(self):
        old = monster('巨獸')
        old['balance_version'] = 2
        old['profile']['speed'] = 0.7
        old['profile'].pop('level_bonus')
        player, enemy = raid_battle([participant()], old, 1).fighters
        self.assertEqual(player.speed, 20)
        self.assertEqual(enemy.speed, 22)

    def test_v3_calibrated_tiers_scale_total_hp_with_party_size(self):
        for kind, level in (('巨獸', 10), ('鐵殼魔像', 20), ('深淵鐘龍', 30)):
            with self.subTest(kind=kind):
                one = participant()
                one['state']['level'] = level
                four = [participant() for _ in range(4)]
                for player in four:
                    player['state']['level'] = level
                solo_enemies = raid_battle([one], monster(kind), 1).living(1)
                party_enemies = raid_battle(four, monster(kind), 1).living(1)
                solo_hp = sum(enemy.stats['HP'] for enemy in solo_enemies)
                party_hp = sum(enemy.stats['HP'] for enemy in party_enemies)
                self.assertEqual(party_hp, solo_hp * 4)
                self.assertEqual(party_enemies[0].stats['攻擊'], solo_enemies[0].stats['攻擊'])

    def test_v3_dynamic_difficulty_scales_hp_attack_and_defense_by_separate_amplitudes(self):
        boosted = monster('巨獸')
        boosted['difficulty_multiplier'] = 2.5
        enemy = raid_battle([participant()], boosted, 1).living(1)[0]
        self.assertEqual((enemy.stats['HP'], enemy.stats['攻擊'], enemy.stats['防禦']),
                         (1957, 241, 27))

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
                    store.db.execute(
                        'INSERT INTO rpg_raid_difficulty(guild_id,channel_id,multiplier,balance_version) '
                        'VALUES (1,12,1.1,2)')
                raid = repo.create(1, 12, monster(quality='精英'), 0, asdict(RaidSettings()))
                # A new balance version starts channel calibration from 1.0.
                self.assertEqual(raid['reward_policy']['victory_xp'], 450)
                self.assertEqual(raid['reward_policy']['victory_gold'], 150)
                self.assertEqual(raid['reward_policy']['drop_chance'], .2)
            finally:
                store.close()
