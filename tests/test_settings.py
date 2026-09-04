import ast
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.settings import DEFAULT_PATH, SettingsError, get_settings, load_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'settings.toml'

    def load(self, content):
        self.path.write_text(content, encoding='utf-8')
        return load_settings(self.path)

    def test_partial_config_defaults_and_nested_overrides(self):
        settings = self.load('[ai]\nreply_chance = 1\n[ai.limits]\nuser_daily_limit = 7\n')
        self.assertEqual(settings.ai.reply_chance, 1.0)
        self.assertIs(type(settings.ai.reply_chance), float)
        self.assertEqual(settings.ai.limits.user_daily_limit, 7)
        self.assertTrue(settings.ai.search.enabled)
        self.assertEqual(settings.ai.memory.summary_model, '')

    def test_invalid_types_ranges_and_unknown_keys(self):
        cases = [
            ('[ai]\nreply_chance = 1.1', 'reply_chance'),
            ('[ai]\nreply_chance = nan', 'reply_chance'),
            ('[ai]\nreply_chance = inf', 'reply_chance'),
            ('[ai]\nreply_chance = true', 'reply_chance'),
            ('[ai.limits]\nuser_daily_limit = -1', 'user_daily_limit'),
            ('[ai.limits]\nuser_daily_limit = true', 'user_daily_limit'),
            ('[ai.search]\nenabled = "false"', 'enabled'),
            ('[ai]\nmodle = "test"', 'modle'),
            ('[ai]\nmodel = " "', 'model'),
            ('[ai]\nlimits = 5', 'limits'),
            ('[tts]\nvoice_id = ""', 'voice_id'),
        ]
        for content, field in cases:
            with self.subTest(content=content):
                with self.assertRaisesRegex(SettingsError, field):
                    self.load(content)

    def test_missing_and_malformed_files_fail(self):
        with self.assertRaises(SettingsError):
            load_settings(self.path)
        with self.assertRaises(SettingsError):
            self.load('[ai')

    def test_environment_does_not_override_behavior(self):
        with patch.dict(os.environ, {'OPENAI_MODEL': 'legacy-model', 'AI_REPLY_CHANCE': '0'}):
            settings = self.load('[ai]\nmodel = "configured-model"\nreply_chance = 0.5')
        self.assertEqual(settings.ai.model, 'configured-model')
        self.assertEqual(settings.ai.reply_chance, 0.5)

    def test_settings_are_immutable(self):
        settings = self.load('')
        with self.assertRaises(FrozenInstanceError):
            settings.ai.limits.user_daily_limit = 99

    def test_shared_startup_snapshot(self):
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        with patch('core.settings.load_settings', return_value=self.load('')) as loader:
            self.assertIs(get_settings(), get_settings())
            loader.assert_called_once_with()

    def test_project_file_and_all_ai_setting_references(self):
        settings = load_settings()
        tree = ast.parse((DEFAULT_PATH.parent.parent / 'cmds/ai.py').read_text(encoding='utf-8'))
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            names = []
            while isinstance(node, ast.Attribute):
                names.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name) and node.id == 'settings':
                value = settings
                for name in reversed(names):
                    value = getattr(value, name)
                count += 1
        self.assertGreater(count, 30)


if __name__ == '__main__':
    unittest.main()
