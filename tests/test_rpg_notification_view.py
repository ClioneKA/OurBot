from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import tempfile
import unittest

from core.rpg import RPGStore
from core.rpg_character import Characters
from core.rpg_farming import Farming
from core.rpg_fishing import Fishing
from core.rpg_notification_view import FarmingNotificationView, FishingNotificationView
from core.settings import RPGSettings


class FixedRandom:
    def random(self):
        return 0.99


def interaction(user_id=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))


class NotificationViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RPGStore(Path(directory.name) / 'rpg.db')
        self.addCleanup(self.store.close)
        self.characters = Characters(self.store, RPGSettings())
        self.fishing = Fishing(self.store, FixedRandom())
        self.farming = Farming(self.store, FixedRandom())
        self.cog = SimpleNamespace(fishing=self.fishing, farming=self.farming)

    async def test_fishing_repeat_claims_once_and_reuses_spot_and_duration(self):
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        view = FishingNotificationView(self.cog, 1, 1, 'pond', 'short', 0.0)
        self.assertTrue(view.is_persistent())
        self.assertEqual([item.label for item in view.children], ['收竿', '收竿並再次釣魚'])
        event = interaction()
        with patch('core.rpg_fishing.time.time', return_value=1800):
            await view.handle(event, True)
        state = self.fishing.state(1, 1)
        self.assertEqual(state['xp'], 200)
        self.assertEqual(state['session']['status'], 'active')
        self.assertEqual((state['session']['spot_id'], state['session']['duration_id']), ('pond', 'short'))
        self.assertEqual(state['session']['ready_at'], 3600)
        event.response.edit_message.assert_awaited_once()
        self.assertIsNone(event.response.edit_message.await_args.kwargs['view'])

    async def test_farming_repeat_harvests_once_and_reuses_plot_and_plant(self):
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        view = FarmingNotificationView(self.cog, 1, 1, 'courtyard', 'potato', 0.0)
        self.assertTrue(view.is_persistent())
        self.assertEqual([item.label for item in view.children], ['收成', '收成並再次種植'])
        event = interaction()
        with patch('core.rpg_farming.time.time', return_value=3600):
            await view.handle(event, True)
        state = self.farming.state(1, 1)
        self.assertEqual(state['xp'], 200)
        self.assertEqual(state['sessions']['courtyard']['status'], 'active')
        self.assertEqual(state['sessions']['courtyard']['plant_id'], 'potato')
        self.assertEqual(state['sessions']['courtyard']['ready_at'], 7200)
        self.assertEqual(self.characters.inventory_counts(1, 1)['farming:potato'], 2)

    async def test_old_dm_cannot_claim_again_after_server_menu_claim(self):
        self.fishing.start(1, 1, 'pond', 'short', now=0)
        view = FishingNotificationView(self.cog, 1, 1, 'pond', 'short', 0.0)
        self.fishing.claim(1, 1, now=1800)
        event = interaction()
        with patch('core.rpg_fishing.time.time', return_value=1801):
            await view.handle(event, True)
        self.assertEqual(self.fishing.state(1, 1)['session']['status'], 'claimed')
        self.assertEqual(self.fishing.state(1, 1)['xp'], 200)
        embed = event.response.edit_message.await_args.kwargs['embed']
        self.assertIn('通知已失效', embed.title)
        self.assertIsNone(event.response.edit_message.await_args.kwargs['view'])

    async def test_only_notification_recipient_can_use_buttons(self):
        self.farming.plant(1, 1, 'courtyard', 'potato', now=0)
        view = FarmingNotificationView(self.cog, 1, 1, 'courtyard', 'potato', 0.0)
        event = interaction(user_id=2)
        await view.handle(event, False)
        event.response.send_message.assert_awaited_once_with(
            '只有收到通知的冒險者可以操作。', ephemeral=True)
        event.response.edit_message.assert_not_awaited()
        self.assertEqual(self.farming.state(1, 1)['xp'], 0)


if __name__ == '__main__':
    unittest.main()
