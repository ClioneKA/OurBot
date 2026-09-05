from dataclasses import asdict
import json
import unittest

from core.rpg_battle import Battle, Fighter, Rule, default_rules, dump_battle, load_battle, raid_battle


def fighter(name='A', team=0, job='民兵', hp=200, dex=10, attack=40, rules=None):
    return Fighter(name, team, job, {'HP': hp, '攻擊': attack, '防禦': 20, '治療量': 60, '命中率': 99, '閃避率': 0, '暴擊率': 0},
                   dex, default_rules(job) if rules is None else rules)


class BattleTests(unittest.TestCase):
    def test_default_arrow_rain_and_bless_rules_avoid_wasted_casts(self):
        archer_rules = default_rules('弓兵')
        cleric_rules = default_rules('僧侶')
        self.assertEqual(archer_rules[2].condition, 'enemies3')
        self.assertEqual(cleric_rules[1].target, 'strongest')

    def test_configurable_numeric_conditions_use_the_saved_threshold(self):
        enemy = fighter('敵人', 1, hp=100, rules=[])
        actor = fighter(hp=100, rules=[Rule(1, 1, True, 'self40', 'lowest', None, 35)])
        battle = Battle([actor, enemy], seed=1)
        actor.hp = 36
        self.assertIsNone(battle.select(actor))
        actor.hp = 35
        self.assertIsNotNone(battle.select(actor))

        actor.rules = [Rule(1, 1, True, 'enemy_hp_lte', 'lowest', None, 25)]
        enemy.hp = 26
        self.assertIsNone(battle.select(actor))
        enemy.hp = 25
        self.assertIsNotNone(battle.select(actor))

        actor.rules = [Rule(1, 1, True, 'round_gte', 'lowest', None, 4)]
        battle.round = 3
        self.assertIsNone(battle.select(actor))
        battle.round = 4
        self.assertIsNotNone(battle.select(actor))

        second_enemy = fighter('敵人 2', 1, hp=100, rules=[])
        battle.fighters.append(second_enemy)
        actor.rules = [Rule(1, 1, True, 'enemies3', 'lowest', None, 2)]
        self.assertIsNotNone(battle.select(actor))

        ally = fighter('隊友', 0, hp=100, rules=[])
        battle.fighters.append(ally)
        actor.rules = [Rule(1, 1, True, 'allies_injured', 'lowest', None, 2)]
        actor.hp, ally.hp = 99, 100
        self.assertIsNone(battle.select(actor))
        ally.hp = 99
        self.assertIsNotNone(battle.select(actor))

    def test_food_triggers_once_and_rare_regen_starts_next_round(self):
        from unittest.mock import patch
        player = fighter(hp=200, rules=[])
        player.food_name = '香酥七彩錦魚'
        player.food_heal_permille = 150
        player.food_regen_permille = 50
        player.food_regen_rounds = 2
        enemy = fighter('敵人', 1, hp=1000, attack=140, rules=[])
        battle = Battle([player, enemy], seed=1)
        battle.hit(enemy, player, precise=True)
        self.assertEqual(player.hp, 97)  # 133 damage, then 15% of 200.
        self.assertTrue(player.food_used)
        self.assertEqual(player.food_regen_left, 2)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        with patch.object(restored, 'act'):
            restored.step()
            self.assertEqual(restored.fighters[0].hp, 107)
            restored.step()
            self.assertEqual(restored.fighters[0].hp, 117)
            restored.step()
            self.assertEqual(restored.fighters[0].hp, 117)

    def test_food_does_not_revive_lethal_damage(self):
        player = fighter(hp=100, rules=[])
        player.food_name = '鯽魚馬鈴薯湯'
        player.food_heal_permille = 150
        enemy = fighter('敵人', 1, hp=1000, attack=200, rules=[])
        enemy.stats['暴擊率'] = 0
        player.stats['防禦'] = 0
        battle = Battle([player, enemy], seed=1)
        battle.hit(enemy, player, precise=True)
        self.assertEqual(player.hp, 0)
        self.assertFalse(player.food_used)

    def test_raid_provisions_apply_potions_and_food_snapshot(self):
        state = dict(job='民兵', level=1,
                     combat={'HP': 101, '攻擊': 101, '防禦': 101, '治療量': 101,
                             '命中率': 149, '閃避率': 39, '暴擊率': 49},
                     total=(1, 1, 1, 1, 1), equipped={'武器': 'starter:club'})
        potion = dict(name='初級專注藥水', stat='命中率', mode='points', amount=3)
        food = dict(name='香酥七彩錦魚', heal_permille=150, regen_permille=50, regen_rounds=2)
        participant = dict(name='玩家', state=state, rules=[], provisions={'food': food, 'potion': potion})
        battle = raid_battle([participant], dict(name='怪物', kind='巨獸'), 1)
        player = battle.fighters[0]
        self.assertEqual(player.stats['命中率'], 150)
        self.assertEqual((player.food_name, player.food_regen_rounds), ('香酥七彩錦魚', 2))
        percent = dict(name='初級生命藥水', stat='HP', mode='percent', amount=5)
        participant['provisions']['potion'] = percent
        player = raid_battle([participant], dict(name='怪物', kind='巨獸'), 1).fighters[0]
        self.assertEqual((player.stats['HP'], player.hp), (106, 106))

    def test_structured_combat_stats_track_actual_values_and_survive_restart(self):
        actor = fighter(attack=100, hp=200, rules=[])
        target = fighter('敵人', 1, hp=50, rules=[])
        target.stats['防禦'] = 0
        battle = Battle([actor, target], seed=1)
        target.hp = 40

        self.assertTrue(battle.hit(actor, target, precise=True))
        self.assertEqual(actor.combat_stats['damage_dealt'], 40)
        self.assertEqual(actor.combat_stats['attacks'], 1)
        self.assertEqual(actor.combat_stats['hits'], 1)
        self.assertEqual(actor.combat_stats['knockouts'], 1)
        self.assertEqual(target.combat_stats['damage_taken'], 40)
        self.assertEqual(target.combat_stats['deaths'], 1)

        actor.hp = 190
        self.assertEqual(battle.heal(actor, actor, 60), 10)
        self.assertEqual(actor.combat_stats['healing_done'], 10)
        self.assertEqual(actor.combat_stats['healing_received'], 10)
        self.assertEqual(actor.combat_stats['overhealing'], 50)

        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(restored.fighters[0].combat_stats, actor.combat_stats)
        legacy = dump_battle(battle)
        legacy['fighters'][0].pop('combat_stats')
        legacy['fighters'][0].pop('user_id')
        restored_legacy = load_battle(legacy).fighters[0]
        self.assertEqual(restored_legacy.combat_stats['damage_dealt'], 0)
        self.assertIsNone(restored_legacy.user_id)

    def test_player_poison_damage_is_credited_after_reload(self):
        from unittest.mock import patch
        player = fighter(rules=[])
        player.user_id = 7
        enemy = fighter('敵人', 1, hp=100, rules=[])
        enemy.hp = 1
        enemy.effects['poison'] = 2
        enemy.effect_sources['poison'] = 7
        battle = load_battle(json.loads(json.dumps(dump_battle(Battle([player, enemy], seed=1)))))

        with patch.object(battle, 'act'):
            battle.step()

        self.assertEqual(battle.fighters[0].combat_stats['damage_dealt'], 1)
        self.assertEqual(battle.fighters[0].combat_stats['knockouts'], 1)
        self.assertEqual(battle.fighters[1].combat_stats['damage_taken'], 1)
        self.assertEqual(battle.fighters[1].combat_stats['deaths'], 1)

    def test_lifesteal_uses_actual_damage_and_survives_restart(self):
        from unittest.mock import patch
        actor = fighter(attack=100, hp=1000, rules=[])
        actor.lifesteal = 2
        actor.hp = 500
        enemy = fighter('敵人', 1, hp=1000, rules=[])
        enemy.stats['防禦'] = 0
        battle = Battle([actor, enemy], seed=1)
        battle.hit(actor, enemy, precise=True)
        self.assertEqual(actor.hp, 502)
        enemy.hp = 50
        battle.hit(actor, enemy, precise=True)
        self.assertEqual(actor.hp, 503)  # Overkill contributes only 50 HP.
        enemy.hp = 49
        battle.hit(actor, enemy, precise=True)
        self.assertEqual(actor.hp, 503)  # No minimum one-HP heal.
        enemy.hp = 1000
        with patch.object(battle.rng, 'random', return_value=0.999):
            self.assertFalse(battle.hit(actor, enemy))
        self.assertEqual(actor.hp, 503)
        actor.hp = 999
        battle.hit(actor, enemy, precise=True)
        self.assertEqual(actor.hp, 1000)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(restored.fighters[0].lifesteal, 2)
        battle.step()
        restored.step()
        self.assertEqual(dump_battle(battle), dump_battle(restored))
        legacy = dump_battle(battle)
        legacy['fighters'][0].pop('lifesteal')
        self.assertEqual(load_battle(legacy).fighters[0].lifesteal, 0)

    def test_fox_attack_evasion_expiry_and_precise_shot(self):
        from unittest.mock import patch
        player = fighter(hp=1000, rules=[])
        fox = fighter('妖狐', 1, job='月影妖狐', rules=[])
        battle = Battle([player, fox], seed=1)
        battle.round = 3
        with patch.object(battle, 'hit') as hit:
            battle.act(fox)
            hit.assert_called_once_with(fox, player, 1.5)
        self.assertTrue(fox.has('moon_shadow', 4))
        self.assertFalse(fox.has('moon_shadow', 5))
        with patch.object(battle.rng, 'random', return_value=0.9):
            self.assertFalse(battle.hit(player, fox))
            self.assertTrue(battle.hit(player, fox, precise=True))
            battle.round = 5
            self.assertTrue(battle.hit(player, fox))
        self.assertEqual(load_battle(dump_battle(battle)).fighters[1].effects, fox.effects)

    def test_bat_bites_every_two_rounds_and_respects_taunt(self):
        from unittest.mock import patch
        player = fighter(hp=1000, rules=[])
        other = fighter('脆皮', hp=1000, rules=[])
        other.hp = 1
        player.effects['taunt'] = 10
        bat = fighter('蝠王', 1, job='血翼蝠王', hp=1000, attack=100, rules=[])
        bat.hp = 500
        battle = Battle([player, other, bat], seed=1)
        for turn in range(1, 5):
            battle.round = turn
            with patch.object(battle, 'hit') as hit:
                battle.act(bat)
                if turn % 2 == 0:
                    hit.assert_called_once_with(bat, player, 1.5, lifesteal=30)
                else:
                    hit.assert_called_once_with(bat, player)
        player.effects.clear()
        other.hp = 0
        player.stats['防禦'] = 0
        player.effects['stance'] = 10
        battle.hit(bat, player, 1.5, precise=True, lifesteal=30)
        self.assertEqual(player.hp, 880)  # Militia reduces 150 damage to 120.
        self.assertEqual(bat.hp, 536)

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

    def test_shield_bash_can_require_and_interrupt_charge(self):
        knight = fighter('騎士', job='騎士', rules=[
            Rule(1, 1, True, 'enemy_charging', 'strongest', 4)
        ])
        golem = fighter('魔像', 1, job='鐵殼魔像', hp=1000, attack=100, rules=[])
        battle = Battle([knight, golem], seed=1)
        battle.round = 1
        self.assertIsNone(battle.select(knight))
        golem.effects['charged_punch'] = 2
        self.assertEqual(battle.select(knight)[1].name, '盾擊')
        battle.act(knight)
        self.assertNotIn('charged_punch', golem.effects)
        self.assertTrue(golem.has('stun', 1))
        self.assertTrue(any('蓄力被打斷' in line for line in battle.log))

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
