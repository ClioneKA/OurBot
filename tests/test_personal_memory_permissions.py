import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cmds.ai import AI
from cmds.anan import Anan
from core.anan_admin_view import AnanAdminView, DeleteMemoryView, AffinityModal
from core.memory import GuildMemory


class PersonalMemoryPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = AI.__new__(AI)
        self.ai._guild_is_allowed = Mock(return_value=True)
        self.ai.memory = Mock()
        self.ai.memory.list_personal_memories.return_value = []
        self.user = Mock(spec=discord.Member)
        self.user.id = 2
        self.user.guild_permissions = discord.Permissions(administrator=True)
        self.other = SimpleNamespace(id=3, display_name='小明')
        self.interaction = SimpleNamespace(
            guild_id=1, user=self.user,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), original_response=AsyncMock(),
        )
        self.panel = AnanAdminView(self.ai, 2, 1)
        self.addCleanup(self.panel.stop)
        self.panel.member = self.other
        self.snapshot = dict(id=7, content='不喝咖啡', basis='explicit', expires_at=None, updated_at='2026-09-11')

    def confirmation(self):
        view = DeleteMemoryView(self.panel, 'personal', 3, dict(self.snapshot))
        self.addCleanup(view.stop)
        return view

    async def test_single_admin_command_is_private_and_old_commands_are_removed(self):
        self.assertTrue(AI.admin_panel.default_permissions.administrator)
        self.assertTrue(AI.admin_panel.guild_only)
        names = {command.name for cls in (AI, Anan) for command in cls.__cog_app_commands__}
        self.assertIn('安安管理', names)
        self.assertFalse(names & {'安安個人記憶', '安安忘記個人記憶', '安安查看印象',
                                 '安安管理好感度', '安安伺服器記憶', '安安刪除伺服器記憶', '上線', '滾', '洗腦'})
        self.assertTrue({'安安叫我', '安安忘記稱呼', '安安好感度', '安安傳話筒'} <= names)
        await AI.admin_panel.callback(self.ai, self.interaction)
        response = self.interaction.response.send_message.call_args
        self.assertTrue(response.kwargs['ephemeral'])
        response.kwargs['view'].stop()

    async def test_member_cannot_open_panel_or_use_existing_delete_confirmation(self):
        self.user.guild_permissions = discord.Permissions.none()
        await AI.admin_panel.callback(self.ai, self.interaction)
        await self.confirmation().confirm.callback(self.interaction)
        self.ai.memory.list_personal_memories.assert_not_called()
        self.ai.memory.forget_personal_memory.assert_not_called()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_other_admin_wrong_guild_and_closed_panel_are_rejected(self):
        self.user.id = 99
        self.assertFalse(await self.panel.interaction_check(self.interaction))
        self.user.id = 2
        self.interaction.guild_id = 4
        self.assertFalse(await self.panel.interaction_check(self.interaction))
        self.interaction.guild_id = 1
        self.panel.stop()
        self.assertFalse(await self.panel.interaction_check(self.interaction))

    async def test_dm_or_disallowed_guild_never_reads_memories(self):
        self.interaction.guild_id = None
        await AI.admin_panel.callback(self.ai, self.interaction)
        self.interaction.guild_id = 1
        self.ai._guild_is_allowed.return_value = False
        await self.panel.refresh(self.interaction)
        self.ai.memory.list_personal_memories.assert_not_called()

    async def test_delete_is_bound_to_original_member_and_cannot_repeat(self):
        self.ai.memory.list_personal_memories.return_value = [self.snapshot]
        confirm = self.confirmation()
        self.panel.member = SimpleNamespace(id=4, display_name='另一人')
        await confirm.confirm.callback(self.interaction)
        self.ai.memory.forget_personal_memory.assert_called_once_with(1, 3, 7)
        await confirm.confirm.callback(self.interaction)
        self.ai.memory.forget_personal_memory.assert_called_once()

    async def test_updated_memory_and_cancel_never_delete(self):
        self.ai.memory.list_personal_memories.return_value = [dict(self.snapshot, content='最近改喝咖啡')]
        await self.confirmation().confirm.callback(self.interaction)
        await self.confirmation().cancel.callback(self.interaction)
        self.ai.memory.forget_personal_memory.assert_not_called()

    async def test_affinity_modal_preserves_target_and_checks_permission_and_range(self):
        self.ai.memory.get_affinity.return_value = 5
        modal = AffinityModal(self.panel, self.other)
        self.panel.member = SimpleNamespace(id=4)
        modal.score._value = '101'
        await modal.on_submit(self.interaction)
        self.ai.memory.set_affinity.assert_not_called()
        modal.score._value = '-12'
        self.user.guild_permissions = discord.Permissions.none()
        await modal.on_submit(self.interaction)
        self.ai.memory.set_affinity.assert_not_called()
        self.user.guild_permissions = discord.Permissions(administrator=True)
        await modal.on_submit(self.interaction)
        self.ai.memory.set_affinity.assert_called_once_with(1, 3, -12)

    async def test_memory_pagination_and_selection_do_not_delete_until_confirmation(self):
        self.panel.section = 'personal'
        self.ai.memory.list_personal_memories.return_value = [dict(self.snapshot, id=i) for i in range(1, 13)]
        embed = self.panel.render()
        self.assertEqual(len(embed.fields), 5)
        picker = next(item for item in self.panel.children if isinstance(item, discord.ui.Select) and item.row == 2)
        picker._values = ['1']
        await picker.callback(self.interaction)
        self.ai.memory.forget_personal_memory.assert_not_called()
        confirmation = self.interaction.response.send_message.call_args.kwargs['view']
        self.assertEqual(confirmation.snapshot['id'], 1)
        confirmation.stop()
        await self.panel.next(self.interaction)
        self.assertEqual(self.panel.page, 1)
        self.assertEqual(self.panel.member.id, 3)
        self.panel.page = 999
        embed = self.panel.render()
        self.assertEqual(self.panel.page, 2)
        self.assertEqual(len(embed.fields), 2)

    async def test_guild_memory_deletion_uses_guild_scope(self):
        item = GuildMemory(7, 'culture', '客廳', 3, 2, '2026-09-11')
        self.ai.memory.list_guild_memories.return_value = [item]
        self.panel.section = 'guild'
        snapshot = self.panel.memories()[0]
        confirm = DeleteMemoryView(self.panel, 'guild', None, snapshot)
        await confirm.confirm.callback(self.interaction)
        self.ai.memory.forget_guild_memory.assert_called_once_with(1, 7)
        self.ai.memory.forget_personal_memory.assert_not_called()

    async def test_all_pages_serialize_within_discord_component_limits(self):
        self.ai.memory.list_personal_memories.return_value = [dict(self.snapshot, id=i, content='字' * 300) for i in range(10)]
        self.ai.memory.list_guild_memories.return_value = []
        self.ai.memory.get_affinity.return_value = 0
        self.ai.memory.get_impression.return_value = '字' * 500
        self.ai.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(voice_client=None))
        for section in ('member', 'personal', 'guild', 'voice'):
            self.panel.section = section
            embed = self.panel.render()
            self.assertLess(len(embed), 6000)
            rows = self.panel.to_components()
            self.assertLessEqual(len(rows), 5)
            for row in rows:
                self.assertLessEqual(len(row['components']), 5)


if __name__ == '__main__':
    unittest.main()
