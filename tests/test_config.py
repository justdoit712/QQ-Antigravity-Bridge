import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_store
from config_store import Config, load_config, save_config


class ConfigTests(unittest.TestCase):
    def test_dpapi_round_trip_and_no_plaintext_secret_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config("123456", "test-secret-不是实际密钥", "model", "中文角色", "user-openid", ["group-openid"])
            with patch.object(config_store, "CONFIG_PATH", path):
                save_config(config)
                self.assertNotIn(config.app_secret, path.read_text(encoding="utf-8"))
                self.assertEqual(load_config(), config)
                self.assertFalse(path.with_suffix(".tmp").exists())

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config_store, "CONFIG_PATH", Path(directory) / "missing.json"):
                self.assertEqual(load_config(), Config())

    def test_corrupt_file_fails_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("sensitive-invalid-content", encoding="utf-8")
            with patch.object(config_store, "CONFIG_PATH", path):
                with self.assertRaises(RuntimeError) as error:
                    load_config()
                self.assertNotIn("sensitive-invalid-content", str(error.exception))

    def test_app_id_requires_ascii_numeric_and_secret_required(self):
        for config in (Config(), Config("１２３", "key"), Config("abc", "key"), Config("123", "")):
            with self.subTest(config=config.app_id), self.assertRaises(ValueError):
                config.validate_qq()
        Config("123", "key").validate_qq()
