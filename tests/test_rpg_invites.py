from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.rpg import RPGStore
from core.rpg_character import CharacterError
from core.rpg_invites import AdventurerInvitations, AdventurerInvitationView, role_ids
from core.rpg_spaces import AdventureSpace


class InvitationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RPGStore(Path(self.directory.name) / 'rpg.db')
        self.role = SimpleNamespace(id=99, managed=False, mention='<@&99>', name='冒險者',
                                    is_default=lambda: False, is_assignable=lambda: True)
        self.member = SimpleNamespace(id=20, bot=False, roles=[], add_roles=AsyncMock(),
                                      remove_roles=AsyncMock())
        self.guild = SimpleNamespace(
            id=1, get_role=lambda role_id: self.role if role_id == 99 else None,
            get_member=lambda user_id: self.member if user_id == 20 else None,
            fetch_member=AsyncMock(), fetch_roles=AsyncMock(return_value=[self.role]),
            me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)))
        self.bot = SimpleNamespace(get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
                                   add_view=Mock())
        def create_character(guild_id, user_id):
            created = self.store.create_player(guild_id, user_id)
            return created
        self.characters = SimpleNamespace(create=Mock(side_effect=create_character))
        self.cog = SimpleNamespace(store=self.store, characters=self.characters, bot=self.bot)
        with patch.dict('os.environ', {'RPG_ADVENTURER_ROLE_IDS': '99'}):
            self.service = AdventurerInvitations(self.cog)

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_targeted_invitation_only_recipient_can_accept_once(self):
        token = self.service.repo.create(1, 10, 20)
        with self.assertRaisesRegex(CharacterError, '其他人'):
            await self.service.accept(token, SimpleNamespace(id=21))
        result = await self.service.accept(token, SimpleNamespace(id=20))
        self.assertIn('角色已建立', result)
        self.assertTrue(self.store.has_player(1, 20))
        self.member.add_roles.assert_awaited_once_with(
            self.role, reason='接受安安大冒險邀請', atomic=True)
        self.characters.create.assert_called_once_with(1, 20)
        with self.assertRaisesRegex(CharacterError, '使用過'):
            await self.service.accept(token, SimpleNamespace(id=20))

    async def test_public_invitation_can_create_multiple_players(self):
        token = self.service.repo.create(1, 10)
        await self.service.accept(token, SimpleNamespace(id=20))
        self.assertIsNone(self.service.repo.get(token)['claimed_at'])
        self.assertTrue(AdventurerInvitationView(self.service, token).is_persistent())

    async def test_character_failure_rolls_back_new_role(self):
        token = self.service.repo.create(1, 10, 20)
        self.characters.create.side_effect = RuntimeError('database failure')
        with self.assertRaisesRegex(RuntimeError, 'database failure'):
            await self.service.accept(token, SimpleNamespace(id=20))
        self.member.remove_roles.assert_awaited_once_with(
            self.role, reason='建立冒險角色失敗，回復身分組', atomic=True)
        self.assertIsNone(self.service.repo.get(token)['claimed_at'])

    def test_invalid_role_configuration(self):
        self.assertEqual(role_ids('99, 100,99'), (99, 100))
        with self.assertRaises(ValueError):
            role_ids('not-an-id')

    async def test_database_managed_role_takes_priority_over_environment(self):
        managed = SimpleNamespace(id=100, managed=False, mention='<@&100>', name='管理角色',
                                  is_default=lambda: False, is_assignable=lambda: True)
        self.guild.get_role = lambda role_id: {99: self.role, 100: managed}.get(role_id)
        self.cog.spaces = SimpleNamespace(store=SimpleNamespace(
            get=lambda guild_id: AdventureSpace(guild_id, adventurer_role_id=100)))
        self.assertIs(await self.service.role_for(self.guild), managed)
