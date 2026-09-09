from collections import Counter
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core.rpg import RPGStore
from core.rpg_battle import dump_battle, load_battle, Tactics
from core.rpg_character import Characters, CharacterError
from core.rpg_monsters import REFERENCE_LEVELS
from core.rpg_painted_maze import COLOR_CONTRACTS, ENTRY_ENABLED, PaintedMazeError
from core.rpg_painted_maze_battle import build_final_battle, build_painting_battle
from core.rpg_painted_maze_service import PaintedMazeService, ContractVoteView
from core.rpg_painted_maze_views import FinalVoteView
from core.rpg_painted_maze_rest import RestTactics, MazeSkillView
from core.settings import RPGSettings
from tests.test_rpg_painted_maze_battle import participant


class MazeLiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RPGStore(Path(temp.name) / 'maze.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.bot = SimpleNamespace(is_ready=lambda: True)
        self.cog = SimpleNamespace(bot=self.bot, store=self.store,
                                   divinations=SimpleNamespace(clear_raid=lambda room_id: None))
        self.service = PaintedMazeService(self.cog)
        self.service._refresh = AsyncMock()
        self.service._post_battle_report = AsyncMock()
        self.service._archive = AsyncMock()
        self.repo = self.service.repo
        for uid in (1, 2):
            self.characters.create(1, uid)
        self.now = time.time()
        self.characters.grant_item(1, 1, 'painting:balloon')
        room = self.repo.create(1, 1, 'painting:balloon', 60, seed=42,
                                require_entry=True, now=self.now)
        self.repo.change_member(room['id'], 2, 60, now=self.now)
        players = [participant(1), participant(2)]
        for player in players:
            player['state']['combat']['HP'] = 20000
        self.room = self.repo.begin(room['id'], 1, players, now=self.now)

    def ready_all(self):
        room = self.repo.get(self.room['id'])
        for uid in room['members']:
            if uid not in room.get('rest_ready', []):
                self.repo.ready_at_rest(room['id'], uid, expected_index=room['boss_index'])

    def reach_final_vote(self):
        room = self.repo.get(self.room['id'])
        for _ in range(3):
            room = self.repo.record_boss_victory(room['id'], 1, now=self.now)
            self.now = room['contract_vote']['deadline']
            room = self.repo.resolve_contract(room['id'], now=self.now)
        self.ready_all()
        return self.repo.ensure_final_vote(room['id'], now=self.now)

    def decide(self, enter):
        room = self.reach_final_vote()
        for uid in room['members']:
            self.repo.vote_final(room['id'], uid, 'enter' if enter else 'retreat', now=self.now)
        self.now = room['final_vote']['deadline']
        return self.repo.resolve_final_vote(room['id'], now=self.now)

    async def test_regular_entry_creation_join_and_start_stay_closed(self):
        self.assertFalse(ENTRY_ENABLED)
        with self.assertRaisesRegex(PaintedMazeError, '暫停開放'):
            await self.service.create(None, 'painting:balloon')
        with self.assertRaisesRegex(PaintedMazeError, '暫停開放'):
            await self.service.change_member(self.room['id'], SimpleNamespace(id=3, bot=False))
        with self.assertRaisesRegex(PaintedMazeError, '暫停開放'):
            await self.service.begin(self.room['id'], SimpleNamespace(id=1))

    async def test_admin_room_can_be_created_joined_and_started_while_regular_entry_closed(self):
        players = {uid: SimpleNamespace(id=uid, bot=False, display_name=f'玩家{uid}')
                   for uid in (3, 4)}
        message = SimpleNamespace(id=102, edit=AsyncMock())
        thread = SimpleNamespace(id=101, add_user=AsyncMock(), send=AsyncMock(return_value=message),
                                 get_partial_message=lambda mid: message)
        channel = SimpleNamespace(send=AsyncMock(return_value=message),
                                  create_thread=AsyncMock(return_value=thread))
        interaction = SimpleNamespace(guild_id=1, channel_id=100, channel=channel, user=players[3])
        self.cog.spaces = SimpleNamespace(store=SimpleNamespace(
            get=lambda gid: SimpleNamespace(maze_channel_id=100)))
        self.cog.characters = SimpleNamespace(snapshot=lambda gid, uid: participant(uid)['state'])
        self.cog.tactics = SimpleNamespace(passive=lambda *args: None, rules=lambda *args: [])
        effects = SimpleNamespace(prepare_for_raid=lambda *args, **kwargs: {})
        self.cog.divinations = self.cog.provisions = effects
        self.cog.tavern = SimpleNamespace(store=effects)
        self.bot.get_guild = lambda gid: SimpleNamespace(get_member=players.get)
        self.bot.get_channel = lambda cid: None
        self.service._thread = AsyncMock(return_value=thread)

        room = await self.service.create(interaction, 'painting:balloon', require_entry=False)
        self.assertFalse(room['requires_entry'])
        joined = await self.service.change_member(room['id'], players[4])
        self.assertEqual(joined['members'], [3, 4])
        started = await self.service.begin(room['id'], players[3])
        self.assertEqual(started['status'], 'running')
        self.assertFalse(started['entry_consumed'])
        self.assertFalse(started['requires_entry'])

    async def test_start_does_not_simulate_and_restart_advances_one_round(self):
        self.ready_all()
        room = await self.service.advance(self.room['id'], SimpleNamespace(id=1))
        self.assertEqual(room['battle']['round'], 0)
        self.assertEqual(room['battle_history'], [])
        with self.assertRaises(PaintedMazeError):
            await self.service.advance(room['id'], SimpleNamespace(id=2))
        restarted = PaintedMazeService(self.cog)
        restarted._refresh = AsyncMock()
        await restarted._step_battle(restarted.repo.get(room['id']))
        saved = self.repo.get(room['id'])
        self.assertEqual(saved['battle']['round'], 1)
        self.assertEqual(saved['boss_index'], 0)
        self.assertIsNone(restarted.room_view(saved))

    async def test_escrow_blocks_early_claim_then_retreat_pays_all_once(self):
        room = self.reach_final_vote()
        self.assertIsInstance(self.service.room_view(room), FinalVoteView)
        with self.assertRaisesRegex(CharacterError, '離場'):
            self.service.crystals.seal_stage(room['id'], 1)
        with self.assertRaisesRegex(CharacterError, '離場'):
            self.service.rewards.seal_currency(room['id'], 1)
        self.service.recover_rewards(room['id'])
        self.assertEqual(self.service.rewards.currency_rewards(room['id']), [])
        # A tie (one yes, one no) protects the full loot.
        self.repo.vote_final(room['id'], 1, 'enter', now=self.now)
        self.repo.vote_final(room['id'], 2, 'retreat', now=self.now)
        room = self.repo.resolve_final_vote(room['id'], now=room['final_vote']['deadline'])
        self.assertEqual(room['status'], 'retreated')
        for _ in range(2):
            room = self.service.recover_rewards(room['id'])
        self.assertEqual(room['reward_due'], [])
        rewards = self.service.rewards.currency_rewards(room['id'])
        self.assertEqual(sum(r['xp'] for r in rewards if r['user_id'] == 1), 1250)
        self.assertEqual(sum(r['gold'] for r in rewards if r['user_id'] == 1), 750)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM rpg_crystal_instances WHERE source_room_id=?', (room['id'],)).fetchone()[0], 6)

    async def test_failure_pays_half_and_retains_two_of_three_crystals_after_restart(self):
        room = self.decide(True)
        battle = build_final_battle(room)
        self.repo.start_battle(room['id'], 1, dump_battle(battle), deadline=self.now + 60)
        self.repo.settle_final(room['id'], 1, '戰敗', dict(round=4), {})
        restarted = PaintedMazeService(self.cog)
        restarted.recover_rewards(room['id'])
        restarted.recover_rewards(room['id'])
        rewards = self.service.rewards.currency_rewards(room['id'])
        self.assertEqual(sum(r['xp'] for r in rewards if r['user_id'] == 1), 625)
        self.assertEqual(sum(r['gold'] for r in rewards if r['user_id'] == 1), 375)
        rows = self.store.db.execute('''SELECT source_user_id,COUNT(*) FROM rpg_crystal_instances
            WHERE source_room_id=? GROUP BY source_user_id''', (room['id'],)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 2), (2, 2)])
        self.assertEqual(self.service.rewards.rewards(room['id']), [])
        self.assertEqual(self.repo.get(room['id'])['loot_percent'], 50)

    async def test_admin_close_after_entry_also_has_risk_and_pre_entry_does_not(self):
        room = self.decide(True)
        battle = build_final_battle(room)
        self.repo.start_battle(room['id'], 1, dump_battle(battle), deadline=self.now + 60)
        room = self.repo.close(room['id'], 99, administrator=True, now=self.now)
        self.assertEqual(room['loot_percent'], 50)

    async def test_pre_entry_close_keeps_loot_and_vote_cannot_be_bypassed(self):
        room = self.reach_final_vote()
        with self.assertRaises(PaintedMazeError):
            await self.service.advance(room['id'], SimpleNamespace(id=1))
        with self.assertRaises(PaintedMazeError):
            self.repo.vote_final(room['id'], 999, 'enter', now=self.now)
        room = self.repo.close(room['id'], 99, administrator=True, now=self.now)
        self.assertEqual(room['loot_percent'], 100)
        self.assertTrue(self.repo.terminal_refreshes())
        self.repo.mark_terminal_refreshed(room['id'])
        self.assertEqual(self.repo.terminal_refreshes(), [])

    async def test_odd_currency_totals_are_halved_once_after_individual_bonuses(self):
        room = self.decide(True)
        room['participants'][0]['meal'] = {'xp_percent': 1, 'gold_percent': 1}
        with self.store.db:
            self.repo._save(room)
        self.repo.settle_final(room['id'], 1, '平手（達回合上限）', {'round': 40}, {})
        self.service.recover_rewards(room['id'])
        rows = [r for r in self.service.rewards.currency_rewards(room['id']) if r['user_id'] == 1]
        self.assertEqual(sum(r['xp'] for r in rows), (252 + 404 + 606) // 2)
        self.assertEqual(sum(r['gold'] for r in rows), (151 + 252 + 353) // 2)

    async def test_final_automatically_steps_and_expiry_keeps_penalty(self):
        room = self.decide(True)
        await self.service.tick.coro(self.service)
        room = self.repo.get(room['id'])
        self.assertEqual(room['battle']['round'], 0)
        self.assertTrue(room['final_entered'])
        self.assertIsNone(self.service.room_view(room))
        self.assertNotIn('choices', room['battle'])
        await self.service._step_battle(room)
        self.assertEqual(self.repo.get(room['id'])['battle']['round'], 1)
        expired = self.repo.expire_due(now=room['expires_at'] + 1)[0]
        self.assertEqual(expired['loot_percent'], 50)

    async def test_contract_catalog_and_discord_field_limits(self):
        self.assertEqual(len(COLOR_CONTRACTS), 18)
        self.assertEqual(set(Counter(c['color'] for c in COLOR_CONTRACTS.values()).values()), {3})
        room = self.room
        room = self.repo.record_boss_victory(room['id'], 1)
        self.assertEqual(len(ContractVoteView(self.service, room).choice.options), 3)
        embed = self.service.room_embed(room)
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_final_rest_displays_third_contract_backlash(self):
        final_room = self.reach_final_vote()
        final_embed = self.service.room_embed(final_room)
        backlash = next(field.value for field in final_embed.fields if field.name.startswith('尾王反噬'))
        third = COLOR_CONTRACTS[final_room['contracts'][2]]
        self.assertIn(f'3. {third["name"]}：{third["backlash"]}', backlash)

    async def test_rest_skill_edits_preserve_frozen_state_and_do_not_change_global_tactics(self):
        import copy
        global_tactics = Tactics(self.store)
        before = copy.deepcopy(self.repo.get(self.room['id'])['participants'][0]['state'])
        adapter = RestTactics(self.repo, self.room['id'], 1, 0)
        self.addCleanup(adapter.db.close)
        self.ready_all()
        adapter.equip(1, 1, before['job'], 1, 4)
        adapter.configure(1, 1, before['job'], 1, 2, True, 'always', 'lowest')
        adapter.equip_passive(1, 1, before['job'], 2)
        room = self.repo.get(self.room['id'])
        player = room['participants'][0]
        self.assertEqual(player['state'], before)
        self.assertEqual(player['passive_id'], 2)
        self.assertEqual(next(r for r in player['rules'] if r['slot'] == 1)['skill_id'], 4)
        self.assertNotIn(1, room['rest_ready'])
        self.assertNotEqual(next(r for r in global_tactics.rules(1, 1, before['job']) if r.slot == 1).skill_id, 4)
        with self.assertRaisesRegex(PaintedMazeError, '全隊'):
            await self.service.advance(room['id'], SimpleNamespace(id=2))
        self.ready_all()
        started = await self.service.advance(room['id'], SimpleNamespace(id=1))
        fighter = next(f for f in load_battle(started['battle']).fighters if f.user_id == 1)
        self.assertEqual(next(r for r in fighter.rules if r.slot == 1).skill_id, 4)
        with self.assertRaisesRegex(PaintedMazeError, '休息點'):
            adapter.configure(1, 1, before['job'], 1, 1, True, 'always', 'lowest')

    async def test_every_battle_has_a_rest_and_previous_panel_cannot_modify_next_rest(self):
        for index in range(3):
            room = self.repo.get(self.room['id'])
            self.assertEqual(room['boss_index'], index)
            self.assertEqual(room['rest_ready'], [])
            self.assertFalse(room.get('final_vote'))
            if index:
                with self.assertRaisesRegex(PaintedMazeError, '已結束'):
                    self.repo.rest_participant(room['id'], 1, expected_index=index - 1)
            self.ready_all()
            room = self.repo.settle_painting(room['id'], 1, index, '勝利', {'round': 1}, room['party_state'])
            self.assertEqual(room['stage'], index + 1)
            self.assertEqual(room['status'], 'contract')
            room = self.repo.resolve_contract(room['id'], now=room['contract_vote']['deadline'])
        self.assertEqual(room['boss_index'], 3)
        self.assertEqual(len(room['contracts']), 3)
        self.assertFalse(room.get('final_vote'))
        self.assertEqual(room['rest_ready'], [])
        with self.assertRaisesRegex(PaintedMazeError, '全隊'):
            await self.service.advance(room['id'], SimpleNamespace(id=1))
        self.ready_all()
        vote = await self.service.advance(room['id'], SimpleNamespace(id=1))
        self.assertTrue(vote['final_vote'])
        self.assertFalse(vote.get('battle'))

    async def test_rest_panel_has_no_job_equipment_or_home_navigation(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1)
        view = MazeSkillView(self.service, self.room, interaction)
        self.addCleanup(view.stop)
        labels = [getattr(item, 'label', None) for item in view.children]
        self.assertNotIn('返回主選單', labels)
        self.assertIn('更換技能', labels)
        self.assertIn('職業、裝備', view.embed().description)
        view.setting_passive = True
        view.rebuild()
        self.assertNotIn('返回主選單', [getattr(item, 'label', None) for item in view.children])

    async def test_actual_skill_panel_saves_room_tactics_and_rejects_stale_callbacks(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=1,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
            followup=SimpleNamespace(send=AsyncMock()))
        view = MazeSkillView(self.service, self.room, interaction)
        self.addCleanup(view.stop)
        await view.handle(interaction, 'equip', '4')
        self.assertEqual(next(r for r in self.repo.get(self.room['id'])['participants'][0]['rules']
                              if r['slot'] == 1)['skill_id'], 4)
        interaction.response.edit_message.assert_awaited_once()
        room = self.repo.record_boss_victory(self.room['id'], 1)
        self.repo.resolve_contract(room['id'], now=room['contract_vote']['deadline'])
        await view.handle(interaction, 'equip', '5')
        interaction.response.send_message.assert_awaited_once()
        self.assertIn('已結束', interaction.response.send_message.call_args.args[0])
        self.assertEqual(next(r for r in self.repo.get(self.room['id'])['participants'][0]['rules']
                              if r['slot'] == 1)['skill_id'], 4)


class MazeAutomaticBalanceTests(unittest.TestCase):
    def test_route_has_three_encounters_at_requested_reference_levels(self):
        from core.rpg_painted_maze import draw_painting_route
        from core.rpg_painted_maze_battle import painting_monster, final_monster
        route = draw_painting_route(1)
        self.assertEqual([p['tier'] for p in route], [55, 60, 65])
        for painting in route:
            monster = painting_monster(painting, 4)
            self.assertEqual(REFERENCE_LEVELS[monster['tier']] + monster['profile']['level_bonus'], painting['tier'])
        for route_name in ('noah', 'shadow'):
            monster = final_monster(dict(route=route_name, participants=[participant()]))
            self.assertEqual(REFERENCE_LEVELS[monster['tier']] + monster['profile']['level_bonus'], 70)

    def test_automatic_final_supports_eight_players_and_reload_matches_next_round(self):
        room = dict(status='running', boss_index=3, seed=5, route='noah',
                    participants=[participant(uid + 1) for uid in range(8)], contracts=['crimson', 'azure', 'gold'])
        battle = build_final_battle(room)
        restored = load_battle(dump_battle(battle))
        battle.step()
        restored.step()
        self.assertEqual(dump_battle(battle), dump_battle(restored))
        self.assertEqual(battle.round, 1)
        self.assertFalse(hasattr(battle, 'choices'))


if __name__ == '__main__':
    unittest.main()
