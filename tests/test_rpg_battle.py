from dataclasses import asdict
import json
import unittest

from core.rpg_battle import Battle, Fighter, Rule, default_rules, dump_battle, load_battle, raid_battle


def fighter(name='A', team=0, job='民兵', hp=200, dex=10, attack=40, rules=None):
    return Fighter(name, team, job, {'HP': hp, '攻擊': attack, '防禦': 20, '治療量': 60, '命中率': 99, '閃避率': 0, '暴擊率': 0},
                   dex, default_rules(job) if rules is None else rules)


class BattleTests(unittest.TestCase):
    def test_tree_stuns_exact_floor_of_living_players_and_heals(self):
        for count, expected in ((1, 0), (3, 0), (4, 1), (6, 1), (10, 3), (20, 6)):
            players = [fighter(str(i), hp=1000) for i in range(count)]
            dead = fighter('倒下')
            dead.hp = 0
            tree = fighter('妖樹', 1, job='荊棘妖樹', hp=1000, rules=[])
            tree.hp = 970
            battle = Battle(players + [dead, tree], seed=1)
            battle.round = 3
            battle.act(tree)
            self.assertEqual(tree.hp, 1000)
            self.assertEqual(sum(f.has('stun', 3) for f in players), expected)
            self.assertFalse(dead.has('stun', 3))
            restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
            self.assertEqual(dump_battle(restored), dump_battle(battle))

    def test_stun_skips_once_and_can_be_cleansed(self):
        from unittest.mock import patch
        actor = fighter('玩家', hp=1000, rules=[])
        actor.effects['stun'] = 2
        enemy = fighter('怪物', 1, rules=[])
        battle = Battle([actor, enemy], seed=1)
        with patch.object(battle, 'act') as act:
            battle.step()
            self.assertNotIn(actor, [call.args[0] for call in act.call_args_list])
            act.reset_mock()
            battle.step()
            self.assertIn(actor, [call.args[0] for call in act.call_args_list])
        healer = fighter('僧侶', job='僧侶', rules=[Rule(3, 1, True, 'ally_debuff', 'debuffed')])
        actor.effects['stun'] = 3
        battle.fighters.insert(0, healer)
        self.assertIs(battle.select(healer)[2], actor)
        battle.act(healer)
        self.assertNotIn('stun', actor.effects)

    def test_cleanse_debuff_target_skips_healthy_dead_and_expired(self):
        healer = fighter('僧侶', job='僧侶', rules=[Rule(3, 1, True, 'ally_debuff', 'debuffed')])
        healthy = fighter('低血量隊友')
        healthy.hp = 1
        poisoned = fighter('中毒隊友')
        poisoned.effects['poison'] = 3
        dead = fighter('倒下隊友')
        dead.hp = 0
        dead.effects['break'] = 3
        battle = Battle([healer, healthy, poisoned, dead, fighter('敵人', 1)], seed=1)
        battle.round = 1
        self.assertIs(battle.select(healer)[2], poisoned)
        battle.act(healer)
        self.assertNotIn('poison', poisoned.effects)
        self.assertIsNone(battle.select(healer))
        healer.ready.clear()
        healthy.effects['poison'] = 0
        self.assertIsNone(battle.select(healer))
        healer.effects['break'] = 3
        self.assertIs(battle.select(healer)[2], healer)

    def test_golem_charge_survives_reload_and_punch_respects_taunt(self):
        from unittest.mock import patch
        tank = fighter('騎士', hp=3000)
        ally = fighter('隊友', hp=1000)
        ally.hp = 100
        golem = fighter('魔像', 1, job='鐵殼魔像', rules=[])
        battle = Battle([tank, ally, golem], seed=1)
        battle.round = 3
        with patch.object(battle, 'hit') as hit:
            battle.act(golem)
            hit.assert_not_called()
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        restored.round = 4
        tank, ally, golem = restored.fighters
        tank.effects['taunt'] = 5
        with patch.object(restored, 'hit') as hit:
            restored.act(golem)
            hit.assert_called_once_with(golem, tank, 2.5)
        self.assertNotIn('charged_punch', golem.effects)
        restored.round = 5
        with patch.object(restored, 'hit') as hit:
            restored.act(golem)
            hit.assert_called_once_with(golem, tank)

    def test_slime_three_hits_taunt_and_stop_when_no_targets(self):
        from unittest.mock import patch
        tank = fighter('騎士', hp=1000)
        tank.effects['taunt'] = 2
        ally = fighter('隊友', hp=1000)
        ally.hp = 100
        slime = fighter('史萊姆群', 1, job='史萊姆群', rules=[])
        battle = Battle([tank, ally, slime], seed=1)
        battle.round = 1
        with patch.object(battle, 'hit', wraps=battle.hit) as hit:
            battle.act(slime)
            self.assertEqual(hit.call_count, 3)
            for call in hit.call_args_list:
                self.assertEqual(call.args, (slime, tank, 0.45))
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        battle.step()
        restored.step()
        self.assertEqual(dump_battle(battle), dump_battle(restored))
        tank.hp = ally.hp = 0
        with patch.object(battle, 'hit') as hit:
            battle.act(slime)
            hit.assert_not_called()

    def test_unarmed_raid_player_cannot_damage_after_reload(self):
        player = fighter()
        state = dict(job='民兵', level=1, combat=player.stats, total=(10,)*5, equipped={})
        battle = raid_battle([dict(name='玩家', state=state, rules=[])], dict(name='魔物', kind='巨獸'), 2)
        battle = load_battle(json.loads(json.dumps(dump_battle(battle))))
        actor, target = battle.fighters
        hp = target.hp
        self.assertFalse(battle.hit(actor, target, precise=True))
        self.assertFalse(battle.hit(actor, target, power=1.6, precise=True))
        self.assertEqual(target.hp, hp)
        self.assertIn('未裝備武器', battle.log[-1])
        self.assertTrue(battle.hit(target, actor, precise=True))
        state['equipped'] = {'武器': 'starter:club'}
        armed = raid_battle([dict(name='玩家', state=state, rules=[])], dict(name='魔物', kind='巨獸'), 2)
        self.assertTrue(armed.hit(*armed.fighters, precise=True))

    def test_weapon_stability_bounds_and_saved_battle(self):
        from unittest.mock import patch
        actor = fighter(attack=100, rules=[])
        actor.stability = (60, 140)
        target = fighter('敵人', 1, hp=1000, rules=[])
        target.stats['防禦'] = 0
        battle = Battle([actor, target], seed=1)
        battle.round = 1
        for percent in (60, 140):
            target.hp = 1000
            with patch.object(battle.rng, 'randint', return_value=percent) as roll:
                battle.hit(actor, target, precise=True)
            self.assertEqual(1000 - target.hp, percent)
            roll.assert_called_once_with(60, 140)
        saved = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(saved.fighters[0].stability, (60, 140))
        battle.step()
        saved.step()
        self.assertEqual(dump_battle(battle), dump_battle(saved))

    def test_legacy_battle_stats_upgrade_without_resetting_progress(self):
        battle = Battle([fighter(job='僧侶'), fighter('敵人', 1)], seed=2)
        data = dump_battle(battle)
        for f in data['fighters']:
            f['stats'].pop('攻擊')
            f['stats'].pop('防禦')
            f['stats'].update(物攻=30, 物防=20, 法攻=100, 法防=50)
            f['hp'] = 77
            f['ready'] = {1: 5}
        restored = load_battle(json.loads(json.dumps(data)))
        self.assertEqual(restored.fighters[0].stats['攻擊'], 80)
        self.assertEqual(restored.fighters[0].stats['防禦'], 20)
        self.assertEqual(restored.fighters[1].stats['防禦'], 20)
        self.assertEqual(restored.fighters[0].hp, 77)
        self.assertEqual(restored.fighters[0].ready, {1: 5})
        self.assertNotIn('法攻', restored.fighters[0].stats)
        self.assertNotIn('法防', restored.fighters[0].stats)
        restored.step()

    def test_knight_mitigation_stacks_and_expires(self):
        attacker = fighter('敵人', 1, attack=200, rules=[])
        target = fighter(job='騎士', hp=1000)
        target.stats['防禦'] = 0
        battle = Battle([target, attacker], seed=1)
        battle.round = 1
        for effects, expected in (({'taunt': 2}, 170), ({'stance': 2}, 100),
                                  ({'taunt': 2, 'stance': 2}, 85)):
            target.effects = effects
            target.hp = 1000
            battle.hit(attacker, target, precise=True)
            self.assertEqual(1000 - target.hp, expected)
        battle.round = 3
        target.hp = 1000
        battle.hit(attacker, target, precise=True)
        self.assertEqual(target.hp, 800)
        for job, expected_hp in (('民兵', 840), ('裝甲步兵', 870)):
            target.job, target.effects, target.hp = job, {'stance': 3}, 1000
            battle.hit(attacker, target, precise=True)
            self.assertEqual(target.hp, expected_hp)

    def test_militia_bandage_is_half_healing_and_caps_at_max_hp(self):
        for job, slot, expected in (('民兵', 2, 30), ('僧侶', 1, 61)):
            healer = fighter(job=job, rules=[Rule(slot, 1, True, 'always', 'lowest')])
            healer.stats['治療量'] = 61
            healer.hp = 10
            battle = Battle([healer, fighter('敵人', 1)], seed=1)
            battle.round = 1
            battle.act(healer)
            self.assertEqual(healer.hp, 10 + expected)
            battle.round = healer.ready[slot]
            healer.hp = healer.stats['HP'] - 1
            battle.act(healer)
            self.assertEqual(healer.hp, healer.stats['HP'])

    def test_guard_scales_from_max_hp_protects_team_and_expires(self):
        knight = fighter(job='騎士', hp=1000, rules=[Rule(2, 1, True, 'always', 'self')])
        knight.hp = 100  # Scaling must not fall when the knight is wounded.
        ally = fighter('隊友', hp=1000)
        fallen = fighter('倒下者')
        fallen.hp = 0
        enemy = fighter('敵人', 1, attack=200, rules=[])
        battle = Battle([knight, ally, fallen, enemy], seed=1)
        battle.round = 1
        battle.act(knight)
        self.assertEqual((knight.guard_bonus, ally.guard_bonus), (50, 50))
        self.assertFalse(fallen.has('guard', 1))
        self.assertFalse(enemy.has('guard', 1))
        ally.hp = 1000
        battle.hit(enemy, ally, precise=True)
        self.assertEqual(1000 - ally.hp, int(200 - (20 + 50) * 0.35))
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(restored.fighters[1].guard_bonus, 50)
        restored.round = 3
        restored.fighters[1].hp = 1000
        restored.hit(restored.fighters[-1], restored.fighters[1], precise=True)
        self.assertEqual(restored.fighters[1].hp, 807)
        self.assertEqual(ally.stats['防禦'], 20)

    def test_guard_uses_strongest_bonus_without_stacking_or_extending(self):
        strong = fighter('強騎士', job='騎士', hp=2000, rules=[Rule(2, 1, True, 'always', 'lowest')])
        weak = fighter('弱騎士', job='騎士', hp=1000, rules=[Rule(2, 1, True, 'always', 'lowest')])
        battle = Battle([strong, weak, fighter('敵人', 1)], seed=1)
        battle.round = 1
        battle.act(weak)
        battle.act(strong)
        self.assertEqual((strong.guard_bonus, weak.guard_bonus), (100, 100))
        weak.ready.clear()
        battle.round = 2
        self.assertIsNone(battle.select(weak))
        self.assertEqual(weak.effects['guard'], 2)
        battle.round = 3
        battle.act(weak)
        self.assertEqual((strong.guard_bonus, weak.guard_bonus), (50, 50))

    def test_priority_condition_and_cooldown(self):
        cleric = fighter(job='僧侶')
        enemy = fighter('B', 1)
        battle = Battle([cleric, enemy], seed=1)
        battle.round = 1
        self.assertEqual(battle.select(cleric)[1].name, '祝福')
        cleric.hp = 50
        self.assertEqual(battle.select(cleric)[1].name, '治療')
        battle.act(cleric)
        self.assertEqual(cleric.hp, 110)
        cleric.hp = 50
        for turn in (2, 3):
            battle.round = turn
            self.assertNotEqual(battle.select(cleric)[1].name, '治療')
        battle.round = 4
        self.assertEqual(battle.select(cleric)[1].name, '治療')

    def test_disabled_skills_fall_back_and_dead_do_not_act(self):
        first = fighter(attack=10000, dex=100, rules=[])
        second = fighter('B', 1, dex=0)
        battle = Battle([first, second], seed=1)
        battle.step()
        self.assertEqual(battle.result, '勝利')
        self.assertTrue(any('普通攻擊' in line for line in battle.log))
        self.assertFalse(any('B 使用' in line for line in battle.log))

    def test_taunt_cleanse_expiration_and_hp_cap(self):
        tank, ally, enemy = fighter(job='騎士'), fighter('C'), fighter('B', 1)
        battle = Battle([tank, ally, enemy], seed=1)
        battle.round = 1
        tank.effects['taunt'] = 2
        ally.hp = 1
        rule = Rule(1, 1, True, 'always', 'lowest')
        self.assertIs(battle.target(enemy, [tank, ally], rule, True), tank)
        battle.round = 3
        self.assertIs(battle.target(enemy, [tank, ally], rule, True), ally)
        cleric = fighter(job='僧侶', rules=[Rule(3, 1, True, 'always', 'lowest')])
        battle.fighters.append(cleric)
        ally.effects.update(poison=5, **{'break': 5})
        battle.act(cleric)
        self.assertEqual(ally.effects, {})
        cleric.rules = [Rule(1, 1, True, 'always', 'lowest')]
        ally.hp = ally.stats['HP'] - 1
        battle.act(cleric)
        self.assertEqual(ally.hp, ally.stats['HP'])

    def test_one_monster_no_npc_and_restart_exact_replay(self):
        f = fighter()
        participant = dict(id=1, name='玩家', state=dict(level=10, job='民兵', combat=f.stats,
                           total=(10, 10, 10, 10, 10)), rules=[asdict(r) for r in f.rules])
        battle = raid_battle([participant], dict(name='怪物', kind='巨獸'), seed=10)
        self.assertEqual(len(battle.fighters), 2)
        self.assertEqual([f.name for f in battle.living(0)], ['玩家'])
        battle.step()
        reloaded = load_battle(json.loads(json.dumps(dump_battle(battle))))
        while battle.result is None:
            battle.step()
            reloaded.step()
        self.assertEqual(dump_battle(battle), dump_battle(reloaded))
        self.assertLessEqual(battle.round, 30)

    def test_poison_death_before_action(self):
        victim = fighter()
        victim.hp = 1
        victim.effects['poison'] = 3
        battle = Battle([victim, fighter('B', 1, dex=0)], seed=2)
        battle.step()
        self.assertEqual(battle.result, '戰敗')
        self.assertFalse(any('使用' in line for line in battle.log))

    def test_poison_monster_five_percent_player_two_percent_and_reload(self):
        from unittest.mock import patch
        for team, cases in ((0, ((1000, 50), (199, 9), (19, 1))),
                            (1, ((1000, 20), (199, 3), (49, 1)))):
            for hp, damage in cases:
                with self.subTest(team=team, hp=hp):
                    victim = fighter(team=team, hp=hp, rules=[])
                    victim.effects['poison'] = 2
                    battle = Battle([victim, fighter(team=1 - team, rules=[])], seed=1)
                    battle = load_battle(json.loads(json.dumps(dump_battle(battle))))
                    victim = battle.fighters[0]
                    with patch.object(battle, 'act'):
                        battle.step()
                        self.assertEqual(victim.hp, hp - damage)
                        battle.step()
                        self.assertEqual(victim.hp, hp - damage * 2)
                        battle.step()
                        self.assertEqual(victim.hp, hp - damage * 2)

    def test_draw_cap_and_precise_hit(self):
        archer = fighter(job='弓兵', attack=1, rules=[Rule(2, 1, True, 'always', 'lowest')])
        archer.stats['命中率'] = 0
        enemy = fighter('B', 1, hp=100000, attack=1, rules=[])
        battle = Battle([archer, enemy], seed=1, max_rounds=1)
        battle.step()
        self.assertLess(enemy.hp, enemy.stats['HP'])
        self.assertIn('回合上限', battle.result)
