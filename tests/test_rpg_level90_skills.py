"""Elite skills resolve through the real battle engine and survive snapshots."""
import unittest

from core.rpg_battle import Battle, Fighter, Rule, SKILLS, dump_battle, load_battle, unlocked_skills


def fighter(job, team=0, user_id=None):
    stats = {'HP': 10000 if team else 1000, '攻擊': 100, '防禦': 100,
             '治療量': 200, '命中率': 200, '閃避率': 0, '暴擊率': 0}
    return Fighter(job, team, job, stats, 10, [], user_id=user_id)


def cast(battle, actor, skill_id, target):
    skill = SKILLS[actor.job][skill_id - 1]
    battle.use_skill(actor, Rule(1, 1, True, skill.condition, 'lowest', skill_id), skill, target)


class EliteSkillTests(unittest.TestCase):
    def test_custom_hp_thresholds_control_skill_selection(self):
        soldier, ally, enemy = fighter('裝甲步兵', user_id=1), fighter('民兵', user_id=2), fighter('魔物', 1, 10)
        soldier.rules = [Rule(1, 1, True, 'self_hp_lte', 'self', 6, 37)]
        battle = Battle([soldier, ally, enemy], seed=1)
        battle.round = 1
        soldier.hp = 380
        self.assertIsNone(battle.select(soldier))
        soldier.hp = 370
        self.assertEqual(battle.select(soldier)[1].name, '戰線重整')

        knight = fighter('騎士', user_id=3)
        knight.rules = [Rule(1, 1, True, 'ally_hp_lte', 'lowest', 6, 73)]
        battle = Battle([knight, ally, enemy], seed=1)
        battle.round = 1
        ally.hp = 740
        self.assertIsNone(battle.select(knight))
        ally.hp = 730
        self.assertEqual(battle.select(knight)[1].name, '守望壁壘')

    def test_unlocks_follow_elite_threshold(self):
        for job in ('裝甲步兵', '騎士', '弓兵', '僧侶'):
            self.assertEqual(len(unlocked_skills(job, 89)), 5)
            self.assertEqual(len(unlocked_skills(job, 90)), 8)
            self.assertEqual(len(unlocked_skills(job, 90, 95)), 5)
            self.assertEqual(len(unlocked_skills(job, 95, 95)), 8)
        self.assertEqual(len(unlocked_skills('民兵', 120)), 3)

    def test_every_new_skill_resolves(self):
        for job in ('裝甲步兵', '騎士', '弓兵', '僧侶'):
            for skill_id in (6, 7, 8):
                with self.subTest(job=job, skill_id=skill_id):
                    actor, ally = fighter(job, user_id=1), fighter('民兵', user_id=2)
                    enemy, other = fighter('魔物', 1, 10), fighter('魔物', 1, 11)
                    battle = Battle([actor, ally, enemy, other], seed=1)
                    battle.round = 1
                    actor.hp, ally.hp = 500, 500
                    target = actor if skill_id == 6 and job in ('裝甲步兵', '騎士', '僧侶') else enemy
                    if job == '僧侶' and skill_id in (7, 8):
                        target = ally if skill_id == 7 else actor
                    cast(battle, actor, skill_id, target)
                    self.assertIn(SKILLS[job][skill_id - 1].name, actor.combat_stats['skills_used'])

    def test_poison_catalyst_uses_one_future_tick_and_keeps_owner(self):
        archer, enemy = fighter('弓兵', user_id=1), fighter('魔物', 1, 10)
        archer.passive_id = 2
        battle = Battle([archer, enemy], seed=2)
        battle.round = 1
        cast(battle, archer, 6, enemy)
        arrows = enemy.status_stacks['poison_arrows']
        self.assertEqual(len(arrows), 2)
        before = archer.combat_stats['damage_dealt']
        cast(battle, archer, 8, enemy)
        self.assertEqual(sum(s['remaining'] for s in arrows), 3)
        self.assertEqual(archer.passive_state.get('grace', 0), 0)
        self.assertGreater(archer.combat_stats['damage_dealt'], before)
        self.assertEqual(enemy.status_stacks['passive_toxicity']['1'], 1)
        restored = load_battle(dump_battle(battle))
        self.assertEqual(sum(s['remaining'] for s in restored.fighters[1].status_stacks['poison_arrows']), 3)

    def test_catalyst_default_waits_for_own_poison(self):
        archer, enemy = fighter('弓兵', user_id=1), fighter('魔物', 1, 10)
        archer.rules = [Rule(1, 1, True, 'own_erosion', 'lowest', 8)]
        battle = Battle([archer, enemy], seed=2)
        battle.round = 1
        self.assertIsNone(battle.select(archer))
        enemy.status_stacks['poison_arrows'] = [dict(source_id=2, damage=10,
                                                    next_round=2, remaining=2)]
        self.assertIsNone(battle.select(archer))
        enemy.status_stacks['poison_arrows'].append(dict(source_id=1, damage=10,
                                                        next_round=2, remaining=2))
        self.assertEqual(battle.select(archer)[1].name, '催蝕箭')

    def test_watch_shield_and_delayed_wave(self):
        knight, ally, enemy = fighter('騎士', user_id=1), fighter('民兵', user_id=2), fighter('魔物', 1, 10)
        knight.passive_id = 2
        battle = Battle([knight, ally, enemy], seed=3)
        battle.round = 1
        cast(battle, knight, 6, knight)
        hp = ally.hp
        battle.apply_damage(ally, 50, direct=True)
        self.assertEqual(ally.hp, hp)
        self.assertEqual(knight.passive_state['watch'], 1)
        self.assertEqual(ally.status_stacks['watch_shield'], 30)

        monk = fighter('僧侶', user_id=3)
        monk.passive_id = 1
        ally.hp = 500
        wave = Battle([monk, ally, enemy], seed=4)
        wave.round = 1
        cast(wave, monk, 6, monk)
        self.assertEqual(ally.hp, 600)
        saved = load_battle(dump_battle(wave))
        saved.step()
        self.assertGreaterEqual(saved.fighters[1].hp, 620)
        self.assertEqual(saved.fighters[0].passive_state.get('grace'), 1)

    def test_watch_defense_uses_stronger_guard_without_extending_it(self):
        knight, ally, enemy = fighter('騎士', user_id=1), fighter('民兵', user_id=2), fighter('魔物', 1, 10)
        battle = Battle([knight, ally, enemy], seed=3)
        battle.round = 1
        ally.effects['guard'] = 1
        ally.guard_bonus = 100
        cast(battle, knight, 6, knight)
        self.assertEqual(ally.effects['guard'], 1)
        self.assertEqual(ally.status_stacks['watch_defense_bonus'], 60)
        self.assertEqual(ally.effects['watch_defense'], 2)
        battle.round = 2
        self.assertFalse(ally.has('guard', battle.round))
        self.assertTrue(ally.has('watch_defense', battle.round))

    def test_adaptive_charge_alternates_only_on_hit(self):
        knight, enemy = fighter('騎士', user_id=1), fighter('魔物', 1, 10)
        knight.passive_id = 3
        battle = Battle([knight, enemy], seed=5)
        battle.round = 1
        cast(battle, knight, 7, enemy)
        self.assertEqual(knight.passive_state['lance_last'], 'shield_bash')
        self.assertEqual(knight.passive_state['lance_stacks'], 1)
        cast(battle, knight, 7, enemy)
        self.assertEqual(knight.passive_state['lance_last'], 'knight_charge')
        self.assertEqual(knight.passive_state['lance_stacks'], 3)

    def test_morning_bless_adds_to_single_bless_without_stacking_itself(self):
        monk, ally, enemy = fighter('僧侶', user_id=1), fighter('民兵', user_id=2), fighter('魔物', 1, 10)
        enemy.stats['防禦'] = 0
        battle = Battle([monk, ally, enemy], seed=6)
        battle.round = 1
        ally.effects['bless'] = 2
        ally.status_stacks['bless_attack_percent'] = 40
        cast(battle, monk, 8, monk)
        self.assertEqual(ally.effects['morning_bless'], 2)
        before = enemy.hp
        battle.hit(ally, enemy, precise=True)
        self.assertEqual(before - enemy.hp, 160)
        cast(battle, monk, 8, monk)
        before = enemy.hp
        battle.hit(ally, enemy, precise=True)
        self.assertEqual(before - enemy.hp, 160)


if __name__ == '__main__':
    unittest.main()
