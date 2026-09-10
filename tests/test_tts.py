import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.settings import SettingsError, TTSSettings
from core.tts import _generate_sound_sync, get_cached_sound


class TTSLanguageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(tts=SimpleNamespace(
            voice_id='test-voice', model='speech-2.6-turbo', language_boost='auto'
        ))
        patcher = patch('core.tts.get_settings', return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_japanese_is_sent_at_payload_top_level(self):
        response = Mock()
        response.json.return_value = {'data': {'audio': '0102'}}
        with patch.dict('os.environ', {'MINIMAX_API_KEY': 'test-key'}), \
                patch('core.tts.requests.post', return_value=response) as post:
            self.assertEqual(_generate_sound_sync('世界', None, 'Japanese'), b'\x01\x02')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['language_boost'], 'Japanese')
        self.assertEqual(payload['text'], '世界')
        self.assertNotIn('language_boost', payload['voice_setting'])

    async def test_cache_separates_identical_kanji_by_language(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.tts.generate_sound', new_callable=AsyncMock) as generate:
            generate.side_effect = [b'japanese-audio', b'chinese-audio']
            japanese = await get_cached_sound('世界', cache_dir=directory, language='Japanese')
            chinese = await get_cached_sound('世界', cache_dir=directory, language='Chinese')
            again = await get_cached_sound('世界', cache_dir=directory, language='Japanese')
            self.assertEqual(japanese, b'japanese-audio')
            self.assertEqual(chinese, b'chinese-audio')
            self.assertEqual(again, japanese)
            self.assertEqual(generate.await_count, 2)

    async def test_configured_language_is_used_unless_overridden(self):
        self.settings.tts.language_boost = 'Japanese'
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.tts.generate_sound', new_callable=AsyncMock, return_value=b'audio') as generate:
            await get_cached_sound('世界', cache_dir=directory)
            generate.assert_awaited_once_with('世界', None, 'Japanese')
            await get_cached_sound('世界', cache_dir=directory, language='auto')
            self.assertEqual(generate.await_args.args, ('世界', None, 'auto'))

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(SettingsError):
            TTSSettings(language_boost='ja')


if __name__ == '__main__':
    unittest.main()
