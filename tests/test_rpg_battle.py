from dataclasses import asdict
import json
import unittest
from unittest.mock import patch

from core.rpg_battle import (PREPARATION_TIMING, SKILLS, Battle, Fighter, Rule, default_rules,
                             dump_battle, load_battle, raid_battle, skill_description)


def fighter(name='A', team=0, job='民兵', hp=200, dex=10, attack=40, rules=None):
    return Fighter(name, team, job, {'HP': hp, '攻擊': attack, '防禦': 20, '治療量': 60, '命中率': 99, '閃避率': 0, '暴擊率': 0},
                   dex, default_rules(job) if rules is None else rules)


class BattleTests(unittest.TestCase):
    def test_twin_turn_schedule_revival_delay_and_restart(self):
        knight = fighter('騎士', job='騎士', hp=10000, dex=100, attack=100, rules=[])
        ally = fighter('隊友', hp=10000, dex=50, attack=100, rules=[])
        red = fighter('赤雷', 1, job='赤雷', hp=1000, dex=55, attack=80, rules=[])
        blue = fighter('蒼炎', 1, job='蒼炎', hp=1000, dex=55, attack=80, rules=[])
        battle = Battle([knight, ally, red, blue], seed=1)
        battle.mechanics.update(twin_base_attacks={'赤雷': 80, '蒼炎': 80},
                                twin_base_speeds={'赤雷': 55, '蒼炎': 55})

        battle.round = 1
        with patch.object(battle, 'hit') as hit:
            battle.act(red)
            battle.act(blue)
            self.assertEqual(len(hit.call_args_list), 1)
            self.assertIs(hit.call_args.args[0], red)
        battle.round = 2
        with patch.object(battle, 'hit') as hit:
            battle.act(red)
            battle.act(blue)
            self.assertEqual(len(hit.call_args_list), 2)
            self.assertTrue(all(call.args[0] is blue and call.args[2] == 0.65
                                for call in hit.call_args_list))
        battle.round = 4
        with patch.object(battle, 'hit') as hit:
            battle.act(blue)
            self.assertTrue(all(call.args[2] == 1.2 for call in hit.call_args_list))

        red.hp = 0
        battle.check_end()
        self.assertEqual((battle.mechanics['twin_revive_job'], battle.mechanics['twin_revive_round']),
                         ('赤雷', 6))
        self.assertEqual((blue.stats['攻擊'], blue.speed), (100, 70))
        battle.use_skill(knight, Rule(1, 1, True, 'always', 'lowest'), SKILLS['騎士'][3], blue)
        self.assertEqual(battle.mechanics['twin_revive_round'], 7)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        restored.round = 7
        restored.process_twin_revival()
        restored_red = next(f for f in restored.fighters if f.job == '赤雷')
        restored_blue = next(f for f in restored.fighters if f.job == '蒼炎')
        self.assertEqual(restored_red.hp, 200)
        self.assertEqual((restored_blue.stats['攻擊'], restored_blue.speed), (80, 55))

    def test_whale_shield_tide_and_interrupt(self):
        knight = fighter('騎士', job='騎士', hp=20000, dex=100, attack=200, rules=[])
        ally = fighter('隊友', hp=20000, dex=50, attack=100, rules=[])
        whale = fighter('吞城鯨', 1, job='吞城鯨', hp=20000, dex=35, attack=100, rules=[])
        whale.stats['防禦'] = 0
        battle = Battle([knight, ally, whale], seed=1)
        battle.mechanics.update(whale_shield=2, whale_tide=0, whale_next_swallow=10)
        battle.round = 1
        battle.hit(knight, whale, 1.0)
        self.assertEqual(battle.mechanics['whale_shield'], 2)
        battle.hit(knight, whale, 1.6)
        self.assertEqual(battle.mechanics['whale_shield'], 1)
        battle.hit(knight, whale, 1.6)
        self.assertEqual(battle.mechanics['whale_shield'], 0)
        self.assertTrue(whale.has('break', 3))

        for turn in range(1, 11):
            battle.round = turn
            battle.whale_act(whale)
        self.assertEqual(battle.mechanics['whale_tide'], 100)
        self.assertTrue(battle.mechanics['whale_swallow_charging'])
        self.assertEqual((knight.speed, ally.speed), (90, 40))
        self.assertEqual((knight.healing_received_percent, ally.healing_received_percent), (-25, -25))
        battle.use_skill(knight, Rule(1, 1, True, 'always', 'lowest'), SKILLS['騎士'][3], whale)
        self.assertFalse(battle.mechanics['whale_swallow_charging'])
        self.assertEqual(battle.mechanics['whale_next_swallow'], 13)

    def test_tier_four_accessory_combat_effects(self):
        attacker = fighter('佩戴者', hp=1000, dex=100, attack=100, rules=[])
        attacker.alternating_damage_percent = 10
        enemy = fighter('敵人', 1, hp=1000, dex=10, attack=100, rules=[])
        enemy.stats['防禦'] = 0
        battle = Battle([attacker, enemy], seed=1)
        battle.round = 1
        battle.hit(attacker, enemy)
        self.assertEqual(enemy.hp, 890)
        enemy.hp = 1000
        battle.round = 2
        battle.hit(attacker, enemy, attack_scope='group')
        self.assertEqual(enemy.hp, 890)

        wearer = fighter('鯨飾佩戴者', hp=1000, dex=100, attack=40, rules=[])
        wearer.defense_conversion = True
        wearer.stats['防禦'] = 20
        monster = fighter('怪物', 1, hp=1000, dex=10, attack=100, rules=[])
        target = fighter('目標', 1, hp=1000, dex=10, attack=10, rules=[])
        target.stats['防禦'] = 0
        conversion = Battle([wearer, monster, target], seed=1)
        conversion.hit(monster, wearer)
        self.assertEqual(wearer.stored_defense_attack, 7)
        conversion = load_battle(json.loads(json.dumps(dump_battle(conversion))))
        wearer, monster, target = conversion.fighters
        self.assertTrue(wearer.defense_conversion)
        self.assertEqual(wearer.stored_defense_attack, 7)
        conversion.hit(wearer, target)
        self.assertEqual((wearer.stored_defense_attack, target.hp), (0, 953))

    def test_noah_colour_transition_composition_and_shield_interrupt(self):
        knight = fighter('騎士', job='騎士', hp=5000, attack=80, rules=[])
        ally = fighter('隊友', hp=5000, attack=80, rules=[])
        noah = fighter('城崎諾亞', 1, job='城崎諾亞', hp=10000, attack=100, rules=[])
        battle = Battle([knight, ally, noah], seed=2)
        battle.mechanics.update(noah_primary_color='yellow', noah_color_index=0,
                                noah_composition=0, noah_draft_charging=False)
        battle.round = 1
        battle.act(noah)
        self.assertEqual(noah.combat_stats['skills_used'], {'黃色顏料罐': 1})
        self.assertLess(knight.hp, knight.stats['HP'])
        self.assertLess(ally.hp, ally.stats['HP'])

        noah.hp = 7000
        battle.round = 2
        battle.act(noah)
        self.assertTrue(battle.mechanics['noah_phase_two'])
        self.assertEqual((battle.mechanics['noah_color_index'], battle.mechanics['noah_composition']), (1, 1))
        battle.round = 3
        battle.act(noah)
        battle.round = 4
        battle.act(noah)
        self.assertTrue(battle.mechanics['noah_draft_charging'])
        self.assertEqual(battle.mechanics['noah_composition'], 3)

        battle.round = 5
        bash = SKILLS['騎士'][3]
        battle.use_skill(knight, Rule(4, 4, True, 'always', 'lowest'), bash, noah)
        self.assertFalse(battle.mechanics['noah_draft_charging'])
        self.assertEqual((battle.mechanics['noah_color_index'], battle.mechanics['noah_composition']), (0, 0))
        self.assertTrue(noah.has('break', 6))
        self.assertIn('被打斷', battle.log[-1])

    def test_regular_noah_takes_poison_but_ignores_stun_outside_draft(self):
        archer = fighter('弓兵', job='弓兵', hp=5000, dex=100, attack=500, rules=[])
        knight = fighter('騎士', job='騎士', hp=5000, dex=100, attack=500, rules=[])
        noah = fighter('城崎諾亞', 1, job='城崎諾亞', hp=10000, attack=100, rules=[])
        noah.stats['閃避率'] = 0
        battle = Battle([archer, knight, noah], seed=1)
        battle.mechanics.update(noah_primary_color='red', noah_color_index=0,
                                noah_composition=0, noah_draft_charging=False)
        battle.round = 1
        before = noah.hp
        battle.use_skill(archer, Rule(1, 1, True, 'always', 'lowest'), SKILLS['弓兵'][4], noah)
        battle.use_skill(knight, Rule(1, 1, True, 'always', 'lowest'), SKILLS['騎士'][3], noah)
        self.assertLess(noah.hp, before)
        self.assertIn('poison_arrows', noah.status_stacks)
        self.assertNotIn('stun', noah.effects)
        self.assertFalse(any('免疫中毒' in line for line in battle.log))
        self.assertTrue(any('免疫暈眩' in line for line in battle.log))

        noah.effects.update(poison=2, stun=2)
        before = noah.hp
        battle.step()
        self.assertLess(noah.hp, before)
        self.assertIn('poison', noah.effects)
        self.assertNotIn('stun', noah.effects)
        self.assertTrue(any('中毒，損失' in line for line in battle.log))
        self.assertTrue(any('沒有跳過行動' in line for line in battle.log))

    def test_clock_dragon_armor_requires_hits_and_survives_restart(self):
        player = fighter('弓兵', job='弓兵', attack=100, rules=[])
        dragon = fighter('深淵鐘龍', 1, job='深淵鐘龍', hp=5000, rules=[])
        battle = Battle([player, dragon], seed=1)
        battle.round = 3
        battle.mechanics['next_clock_charge'] = 3
        battle.act(dragon)
        self.assertEqual(battle.mechanics['clock_armor'], 2)
        battle.hit(player, dragon, precise=True)
        self.assertEqual(battle.mechanics['clock_armor'], 0)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        restored.round = 4
        restored.act(restored.fighters[1])
        self.assertTrue(restored.fighters[1].has('break', 5))
        self.assertFalse(restored.mechanics['clock_charging'])
        self.assertIn('鐘甲崩解', restored.log[-1])

    def test_puppeteer_repairs_and_absorbs_living_puppets(self):
        player = fighter(rules=[])
        master = fighter('傀儡師', 1, job='王城傀儡師', hp=1000, attack=100, rules=[])
        sword = fighter('劍傀儡', 1, job='劍傀儡', hp=400, rules=[])
        curse = fighter('咒傀儡', 1, job='咒傀儡', hp=400, rules=[])
        battle = Battle([player, master, sword, curse], seed=1)
        battle.mechanics['puppet_base_attack'] = 100
        sword.hp = 0
        battle.round = 3
        battle.act(master)
        self.assertEqual(sword.hp, 120)
        master.hp = 500
        battle.round = 4
        battle.act(master)
        self.assertTrue(battle.mechanics['puppet_phase_two'])
        self.assertEqual((sword.hp, curse.hp), (0, 0))
        self.assertEqual((master.stats['攻擊'], master.lifesteal), (125, 20))

    def test_corruption_explodes_and_cleanse_recognizes_it(self):
        player = fighter('玩家', hp=1000, dex=100, rules=[])
        ally = fighter('隊友', hp=1000, dex=90, rules=[])
        beast = fighter('縫合獸', 1, job='瘟疫縫合獸', hp=10000, dex=1, rules=[])
        battle = Battle([player, ally, beast], seed=1)
        player.status_stacks['corruption'] = 3
        with patch.object(battle, 'act'):
            battle.step()
        self.assertNotIn('corruption', player.status_stacks)
        self.assertEqual((player.hp, ally.hp, beast.hp), (880, 970, 9850))

    def test_tier_three_equipment_combat_triggers(self):
        wearer = fighter('佩戴者', hp=1000, rules=[])
        ally = fighter('隊友', hp=1000, rules=[])
        enemy = fighter('敵人', 1, hp=5000, attack=200, rules=[])
        battle = Battle([wearer, ally, enemy], seed=1)
        wearer.damage_guard_chance = 100
        battle.hit(enemy, wearer, precise=True)
        self.assertEqual(wearer.hp, 904)
        wearer.healing_share = 10
        ally.hp = 500
        battle.heal(wearer, wearer, 100)
        self.assertEqual(ally.hp, 509)
        wearer.vulnerable_chance = 100
        wearer.vulnerable_percent = 10
        battle.hit(wearer, enemy, precise=True)
        before = enemy.hp
        battle.hit(wearer, enemy, precise=True)
        self.assertEqual(before - enemy.hp, 36)

    def test_default_arrow_rain_and_bless_rules_avoid_wasted_casts(self):
        archer_rules = default_rules('弓兵')
        cleric_rules = default_rules('僧侶')
        self.assertEqual(archer_rules[2].condition, 'enemies3')
        self.assertEqual(cleric_rules[1].target, 'strongest')

    def test_preparation_is_a_visible_data_driven_skill_keyword(self):
        preparation = {skill.name for skills in SKILLS.values() for skill in skills
                       if skill.timing == PREPARATION_TIMING}
        self.assertEqual(preparation, {'防禦', '破甲', '攻守架勢', '挑釁反擊', '護衛', '祝福'})
        self.assertTrue(skill_description(SKILLS['騎士'][0]).startswith('【準備】'))
        self.assertFalse(skill_description(SKILLS['僧侶'][0]).startswith('【準備】'))

    def test_slow_support_skills_resolve_before_fast_attacks(self):
        tank = fighter('騎士', job='騎士', dex=1,
                       rules=[Rule(1, 1, True, 'always', 'self')])
        ally = fighter('隊友', dex=20, rules=[])
        ally.hp = 100
        enemy = fighter('敵人', 1, hp=500, dex=100, attack=40, rules=[])
        battle = Battle([tank, ally, enemy], seed=1)
        battle.step()
        self.assertLess(tank.hp, tank.stats['HP'])
        self.assertEqual(ally.hp, 100)
        self.assertLess(battle.log.index('騎士 使用【挑釁反擊】'), battle.log.index('敵人 使用普通攻擊'))

        cleric = fighter('僧侶', job='僧侶', dex=1,
                         rules=[Rule(2, 1, True, 'always', 'strongest')])
        attacker = fighter('輸出', dex=20, attack=100, rules=[])
        target = fighter('木樁', 1, hp=500, dex=100, attack=1, rules=[])
        battle = Battle([cleric, attacker, target], seed=1)
        battle.step()
        self.assertEqual(target.combat_stats['damage_taken'], 118)
        self.assertLess(battle.log.index('僧侶 使用【祝福】'), battle.log.index('輸出 使用普通攻擊'))

    def test_legacy_battle_snapshot_migrates_dexterity_to_speed(self):
        battle = Battle([fighter(rules=[]), fighter('敵人', 1, rules=[])], seed=1)
        data = dump_battle(battle)
        for saved in data['fighters']:
            saved['dexterity'] = saved.pop('speed')
        restored = load_battle(data)
        self.assertEqual([actor.speed for actor in restored.fighters], [10, 10])

    def test_tank_professions_use_higher_defense_effectiveness(self):
        results = {}
        for job in ('民兵', '裝甲步兵', '騎士', '弓兵', '僧侶'):
            actor = fighter(attack=200, rules=[])
            target = fighter(job, 1, job=job, hp=1000, rules=[])
            target.stats['防禦'] = 100
            Battle([actor, target], seed=1).hit(actor, target, precise=True)
            results[job] = target.combat_stats['damage_taken']
        self.assertEqual(results, {'民兵': 165, '裝甲步兵': 160, '騎士': 155,
                                   '弓兵': 165, '僧侶': 165})

    def test_break_ignores_defense_and_credits_its_source(self):
        caster = fighter('裝甲步兵', job='裝甲步兵', rules=[])
        caster.user_id = 1
        attacker = fighter('隊友', attack=200, rules=[])
        attacker.user_id = 2
        target = fighter('敵人', 1, hp=1000, rules=[])
        target.stats['防禦'] = 100
        target.effects['break'] = 2
        target.effect_sources['break'] = caster.user_id
        battle = Battle([caster, attacker, target], seed=1)
        battle.round = 1

        battle.hit(attacker, target, precise=True)

        self.assertEqual(target.combat_stats['damage_taken'], 200)
        self.assertEqual(attacker.combat_stats['direct_damage'], 165)
        self.assertEqual(caster.combat_stats['support_damage'], 35)

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

    def test_subject_conditions_constrain_the_selected_target(self):
        actor = fighter(rules=[Rule(1, 1, True, 'enemy_hp_lte', 'strongest', None, 30)])
        low = fighter('低血量', 1, hp=100, attack=10, rules=[])
        strong = fighter('高攻擊', 1, hp=100, attack=100, rules=[])
        low.hp, strong.hp = 20, 90
        battle = Battle([actor, low, strong], seed=1)
        self.assertIs(battle.select(actor)[2], low)

        actor.rules = [Rule(1, 1, True, 'enemy_charging', 'lowest')]
        strong.effects['charging'] = 2
        self.assertIs(battle.select(actor)[2], strong)
        low.effects['taunt'] = 2
        self.assertIsNone(battle.select(actor))

    def test_new_enemy_conditions_and_priority_targets(self):
        actor = fighter(rules=[])
        boss = fighter('首領', 1, hp=100, attack=50, rules=[])
        add = fighter('召喚物', 1, hp=100, attack=20, rules=[])
        urgent = fighter('法器', 1, hp=100, attack=10, rules=[])
        boss.is_boss = True
        urgent.mechanic_priority = 2
        battle = Battle([actor, boss, add, urgent], seed=1)
        battle.round = 1

        self.assertIs(battle.target(actor, [boss, add], Rule(1, 1, True, 'always', 'boss'), True), boss)
        self.assertIs(battle.target(actor, [boss, add], Rule(1, 1, True, 'always', 'add'), True), add)
        self.assertIs(battle.target(actor, [boss, add, urgent],
                                    Rule(1, 1, True, 'always', 'mechanic'), True), urgent)
        boss.hp, add.hp = 30, 90
        self.assertIs(battle.target(actor, [boss, add],
                                    Rule(1, 1, True, 'always', 'highest_hp'), True), add)

        for condition, setup, expected in (
                ('enemy_guard', lambda: urgent.effects.update(breakable_guard=2), urgent),
                ('enemy_broken', lambda: add.effects.update({'break': 2}), add),
                ('enemy_add', lambda: None, add),
                ('mechanic_target', lambda: None, urgent)):
            setup()
            actor.rules = [Rule(1, 1, True, condition, 'lowest')]
            selected = battle.select(actor)
            self.assertIsNotNone(selected, condition)
            if condition != 'enemy_add':
                self.assertIs(selected[2], expected)
            else:
                self.assertFalse(selected[2].is_boss)

    def test_stacked_debuff_condition_filters_cleanse_target_and_tags_survive_reload(self):
        monk = fighter('僧侶', job='僧侶', rules=[
            Rule(3, 1, True, 'ally_debuff_stacks', 'strongest', None, 2)])
        one = fighter('一層', attack=100, rules=[])
        two = fighter('兩層', attack=10, rules=[])
        one.status_stacks['corruption'] = 1
        two.status_stacks['corruption'] = 2
        enemy = fighter('首領', 1, rules=[])
        enemy.is_boss = True
        enemy.mechanic_priority = 3
        battle = Battle([monk, one, two, enemy], seed=1)
        self.assertIs(battle.select(monk)[2], two)

        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        restored_enemy = restored.fighters[-1]
        self.assertTrue(restored_enemy.is_boss)
        self.assertEqual(restored_enemy.mechanic_priority, 3)

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
        self.assertEqual(player.stats['命中率'], 152)
        self.assertEqual((player.food_name, player.food_regen_rounds), ('香酥七彩錦魚', 2))
        percent = dict(name='初級生命藥水', stat='HP', mode='percent', amount=5)
        participant['provisions']['potion'] = percent
        player = raid_battle([participant], dict(name='怪物', kind='巨獸'), 1).fighters[0]
        self.assertEqual((player.stats['HP'], player.hp), (106, 106))

    def test_tavern_meal_applies_combat_stats_and_lifesteal(self):
        state = dict(job='民兵', level=1,
                     combat={'HP': 100, '攻擊': 100, '防禦': 50, '治療量': 80,
                             '命中率': 100, '閃避率': 10, '暴擊率': 10},
                     total=(1, 1, 1, 1, 1), equipped={'武器': 'starter:club'})
        meal = dict(name='精緻滋養料理', hp_percent=8, healing_percent=8,
                    attack_percent=6, critical_points=3, lifesteal_percent=4)
        participant = dict(name='玩家', state=state, rules=[], meal=meal)
        player = raid_battle([participant], dict(name='怪物', kind='巨獸'), 1).fighters[0]
        self.assertEqual((player.stats['HP'], player.hp), (108, 108))
        self.assertEqual((player.stats['攻擊'], player.stats['治療量']), (106, 86))
        self.assertEqual((player.stats['暴擊率'], player.lifesteal), (13, 4))

    def test_structured_combat_stats_track_actual_values_and_survive_restart(self):
        actor = fighter(attack=100, hp=200, rules=[])
        target = fighter('敵人', 1, hp=50, rules=[])
        target.stats['防禦'] = 0
        battle = Battle([actor, target], seed=1)
        target.hp = 40

        self.assertTrue(battle.hit(actor, target, precise=True))
        self.assertEqual(actor.combat_stats['damage_dealt'], 40)
        self.assertEqual(actor.combat_stats['direct_damage'], 40)
        self.assertEqual(actor.combat_stats['support_damage'], 0)
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

        old_stats = dump_battle(battle)
        old_stats['fighters'][0]['combat_stats'].pop('direct_damage')
        old_stats['fighters'][0]['combat_stats'].pop('support_damage')
        restored_old_stats = load_battle(old_stats).fighters[0]
        self.assertEqual(restored_old_stats.combat_stats['direct_damage'],
                         restored_old_stats.combat_stats['damage_dealt'])
        self.assertEqual(restored_old_stats.combat_stats['support_damage'], 0)

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
        self.assertEqual(battle.fighters[0].combat_stats['direct_damage'], 0)
        self.assertEqual(battle.fighters[0].combat_stats['support_damage'], 1)
        self.assertEqual(battle.fighters[0].combat_stats['knockouts'], 1)
        self.assertEqual(battle.fighters[1].combat_stats['damage_taken'], 1)
        self.assertEqual(battle.fighters[1].combat_stats['deaths'], 1)

    def test_bless_bonus_is_attributed_as_support_damage_without_double_counting(self):
        caster = fighter('施法者', hp=100, rules=[])
        caster.user_id = 1
        attacker = fighter('攻擊者', hp=100, attack=100, rules=[])
        attacker.user_id = 2
        target = fighter('敵人', 1, hp=1000, rules=[])
        target.stats['防禦'] = 0
        battle = Battle([caster, attacker, target], seed=1)
        battle.round = 1
        attacker.effects['bless'] = 2
        attacker.effect_sources['bless'] = caster.user_id

        battle.hit(attacker, target, precise=True)

        self.assertEqual(attacker.combat_stats['damage_dealt'], 125)
        self.assertEqual(attacker.combat_stats['direct_damage'], 100)
        self.assertEqual(caster.combat_stats['support_damage'], 25)
        self.assertEqual(attacker.combat_stats['direct_damage'] + caster.combat_stats['support_damage'], 125)

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
        tank = fighter('騎士', job='騎士', hp=1000)
        tank.effects['taunt'] = 2
        ally = fighter('隊友', hp=1000)
        ally.hp = 100
        slime = fighter('史萊姆群', 1, job='史萊姆群', rules=[])
        battle = Battle([tank, ally, slime], seed=1)
        battle.round = 1
        with patch.object(battle, 'hit', wraps=battle.hit) as hit:
            battle.act(slime)
            self.assertEqual(hit.call_count, 6)
            for call in hit.call_args_list[::2]:
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

    def test_final_hit_chance_has_no_upper_cap(self):
        actor = fighter(rules=[])
        target = fighter('敵人', 1, rules=[])
        actor.stats['命中率'] = 180
        target.stats['閃避率'] = 20
        battle = Battle([actor, target], seed=1)
        self.assertEqual(battle.hit_chance(actor, target), 160)

    def test_critical_damage_is_per_fighter_and_defaults_to_one_hundred_fifty_percent(self):
        first = fighter('甲', attack=100, hp=1000, rules=[])
        second = fighter('乙', attack=100, hp=1000, rules=[])
        target = fighter('敵人', 1, hp=1000, rules=[])
        target.stats['防禦'] = 0
        first.stats['暴擊率'] = second.stats['暴擊率'] = 100
        second.critical_damage_percent = 175
        battle = Battle([first, second, target], seed=1)
        battle.hit(first, target, precise=True)
        self.assertEqual(target.hp, 850)
        target.hp = 1000
        battle.hit(second, target, precise=True)
        self.assertEqual(target.hp, 825)
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual([fighter.critical_damage_percent for fighter in restored.fighters],
                         [150, 175, 150])
        legacy = dump_battle(battle)
        legacy['fighters'][0].pop('critical_damage_percent')
        self.assertEqual(load_battle(legacy).fighters[0].critical_damage_percent, 150)

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

    def test_knight_taunt_counters_direct_hits_and_expires(self):
        attacker = fighter('敵人', 1, attack=200, rules=[])
        target = fighter(job='騎士', hp=1000)
        target.stats['防禦'] = 0
        target.stats['攻擊'] = 100
        battle = Battle([target, attacker], seed=1)
        battle.round = 1
        target.effects = {'taunt': 2}
        battle.hit(attacker, target, precise=True)
        self.assertEqual(target.hp, 800)
        self.assertLess(attacker.hp, attacker.stats['HP'])
        self.assertTrue(any('挑釁反擊' in line for line in battle.log))
        battle.round = 3
        target.hp = 1000
        attacker.hp = attacker.stats['HP']
        battle.hit(attacker, target, precise=True)
        self.assertEqual(target.hp, 800)
        self.assertEqual(attacker.hp, attacker.stats['HP'])

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
        self.assertEqual((knight.guard_bonus, ally.guard_bonus), (20, 20))
        self.assertTrue(knight.has('immunity', 1))
        self.assertTrue(ally.has('immunity', 1))
        self.assertFalse(fallen.has('guard', 1))
        self.assertFalse(enemy.has('guard', 1))
        ally.hp = 1000
        battle.hit(enemy, ally, precise=True)
        self.assertEqual(1000 - ally.hp, int(200 - (20 + 20) * 0.35))
        restored = load_battle(json.loads(json.dumps(dump_battle(battle))))
        self.assertEqual(restored.fighters[1].guard_bonus, 20)
        restored.round = 3
        restored.fighters[1].hp = 1000
        restored.hit(restored.fighters[-1], restored.fighters[1], precise=True)
        self.assertEqual(restored.fighters[1].hp, 807)
        self.assertEqual(ally.stats['防禦'], 20)

    def test_guard_clears_and_blocks_cleansable_debuffs(self):
        knight = fighter('騎士', job='騎士', rules=[Rule(2, 1, True, 'always', 'lowest')])
        ally = fighter('隊友')
        ally.effects.update(poison=3, weak=3)
        ally.status_stacks['corruption'] = 2
        ally.status_stacks['poison_arrows'] = [
            {'source_id': None, 'damage': 70, 'next_round': 2, 'remaining': 2}]
        battle = Battle([knight, ally, fighter('敵人', 1, rules=[])], seed=1)
        battle.round = 1
        battle.act(knight)
        self.assertNotIn('poison', ally.effects)
        self.assertNotIn('weak', ally.effects)
        self.assertNotIn('corruption', ally.status_stacks)
        self.assertNotIn('poison_arrows', ally.status_stacks)
        self.assertFalse(battle.apply_debuff(ally, 'stun', 2))
        battle.add_corruption(ally)
        self.assertNotIn('stun', ally.effects)
        self.assertNotIn('corruption', ally.status_stacks)

    def test_hindering_shot_reduces_attack_twenty_percent(self):
        archer = fighter('弓兵', job='弓兵', attack=100,
                         rules=[Rule(2, 1, True, 'always', 'lowest')])
        enemy = fighter('敵人', 1, attack=100, hp=1000, rules=[])
        victim = fighter('木樁', hp=1000, rules=[])
        victim.stats['防禦'] = 0
        battle = Battle([archer, victim, enemy], seed=1)
        battle.round = 1
        battle.act(archer)
        self.assertTrue(enemy.has('weak', 1))
        before = victim.hp
        battle.hit(enemy, victim, precise=True)
        self.assertEqual(before - victim.hp, 80)

    def test_guard_uses_strongest_bonus_without_stacking_or_extending(self):
        strong = fighter('強騎士', job='騎士', hp=2000, rules=[Rule(2, 1, True, 'always', 'lowest')])
        weak = fighter('弱騎士', job='騎士', hp=1000, rules=[Rule(2, 1, True, 'always', 'lowest')])
        strong.stats['防禦'] = 100
        weak.stats['防禦'] = 50
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

    def test_monster_targets_randomly_and_taunt_limits_candidates(self):
        from unittest.mock import patch
        tank, ally, enemy = fighter(job='騎士'), fighter('C'), fighter('B', 1)
        battle = Battle([tank, ally, enemy], seed=1)
        battle.round = 1
        rule = Rule(1, 1, True, 'always', 'lowest')
        with patch.object(battle.rng, 'choice', return_value=ally) as choice:
            self.assertIs(battle.target(enemy, [tank, ally], rule, True), ally)
            choice.assert_called_once_with([tank, ally])
        tank.effects['taunt'] = 2
        with patch.object(battle.rng, 'choice', return_value=tank) as choice:
            self.assertIs(battle.target(enemy, [tank, ally], rule, True), tank)
            choice.assert_called_once_with([tank])

    def test_taunt_cleanse_expiration_and_hp_cap(self):
        tank, ally, enemy = fighter(job='騎士'), fighter('C'), fighter('B', 1)
        battle = Battle([tank, ally, enemy], seed=1)
        battle.round = 1
        tank.effects['taunt'] = 2
        ally.hp = 1
        rule = Rule(1, 1, True, 'always', 'lowest')
        self.assertIs(battle.target(enemy, [tank, ally], rule, True), tank)
        battle.round = 3
        self.assertIn(battle.target(enemy, [tank, ally], rule, True), (tank, ally))
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

    def test_draw_cap_and_attack_hit(self):
        archer = fighter(job='弓兵', attack=1, rules=[Rule(2, 1, True, 'always', 'lowest')])
        archer.stats['命中率'] = 100
        enemy = fighter('B', 1, hp=100000, attack=1, rules=[])
        battle = Battle([archer, enemy], seed=1, max_rounds=1)
        battle.step()
        self.assertLess(enemy.hp, enemy.stats['HP'])
        self.assertIn('回合上限', battle.result)
