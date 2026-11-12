import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_store
from config_store import Config, load_config, save_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("QQBOT_APP_ID", "QQBOT_APP_SECRET", "QQBOT_MODEL", "QQBOT_PERSONA"):
            os.environ.pop(name, None)

    @unittest.skipUnless(config_store.IS_WINDOWS, "Requires Windows DPAPI")
    def test_dpapi_round_trip_and_no_plaintext_secret_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config("123456", "test-secret-不是实际密钥", "model", "中文角色", "user-openid", ["group-openid"])
            with patch.object(config_store, "CONFIG_PATH", path):
                save_config(config)
                self.assertNotIn(config.app_secret, path.read_text(encoding="utf-8"))
                self.assertEqual(load_config(), config)
                self.assertFalse(path.with_suffix(".tmp").exists())

    def test_environment_config_without_file(self):
        environment = {
            "QQBOT_APP_ID": "123456",
            "QQBOT_APP_SECRET": "environment-secret",
            "QQBOT_MODEL": "server-model",
            "QQBOT_PERSONA": "服务器角色",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with patch.object(config_store, "CONFIG_PATH", path), patch.dict(os.environ, environment):
                self.assertEqual(load_config(), Config("123456", "environment-secret", "server-model", "服务器角色"))
                self.assertFalse(path.exists())

    def test_linux_persists_state_without_secret(self):
        config = Config("123456", "environment-secret", "model", "中文角色", "owner", ["group"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".local" / "config.json"
            with patch.object(config_store, "CONFIG_PATH", path), \
                    patch.object(config_store, "IS_WINDOWS", False), \
                    patch.object(config_store, "_crypt") as crypt, \
                    patch.dict(os.environ, {"QQBOT_APP_SECRET": config.app_secret}):
                save_config(config)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(config.app_secret, text)
                self.assertNotIn("app_secret_dpapi", json.loads(text))
                self.assertEqual(load_config(), config)
                self.assertEqual(list(path.parent.glob("*.tmp")), [])
                crypt.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Requires POSIX permissions")
    def test_linux_state_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".local" / "config.json"
            with patch.object(config_store, "CONFIG_PATH", path):
                save_config(Config("123456", owner_id="owner"))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_linux_requires_environment_for_copied_windows_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"app_id": "123456", "app_secret_dpapi": "private-encrypted-value"}),
                            encoding="utf-8")
            with patch.object(config_store, "CONFIG_PATH", path), \
                    patch.object(config_store, "IS_WINDOWS", False), \
                    patch.object(config_store, "_crypt") as crypt:
                with self.assertRaisesRegex(RuntimeError, "QQBOT_APP_SECRET") as error:
                    load_config()
                self.assertNotIn("private-encrypted-value", str(error.exception))
                crypt.assert_not_called()
                with patch.dict(os.environ, {"QQBOT_APP_SECRET": "environment-secret"}):
                    self.assertEqual(load_config().app_secret, "environment-secret")
                    crypt.assert_not_called()

    def test_environment_bot_change_clears_previous_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"app_id": "123456", "owner_id": "owner", "group_ids": ["group"]}),
                            encoding="utf-8")
            with patch.object(config_store, "CONFIG_PATH", path):
                with patch.dict(os.environ, {"QQBOT_APP_ID": "123456"}):
                    self.assertEqual(load_config().owner_id, "owner")
                    self.assertEqual(load_config().group_ids, ["group"])
                with patch.dict(os.environ, {"QQBOT_APP_ID": "654321"}):
                    config = load_config()
                    self.assertEqual(config.app_id, "654321")
                    self.assertEqual(config.owner_id, "")
                    self.assertEqual(config.group_ids, [])

    def test_failed_atomic_save_preserves_existing_state_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            previous = '{"app_id": "123456", "owner_id": "existing-owner"}'
            path.write_text(previous, encoding="utf-8")
            with patch.object(config_store, "CONFIG_PATH", path), \
                    patch.object(config_store, "IS_WINDOWS", False), \
                    patch.object(config_store.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_config(Config("123456", owner_id="new-owner"))
                self.assertEqual(path.read_text(encoding="utf-8"), previous)
                self.assertEqual(list(path.parent.glob("*.tmp")), [])

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
