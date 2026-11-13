import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config_store import Config

try:
    from desktop import BridgeWindow
except ImportError:
    BridgeWindow = None


@unittest.skipIf(BridgeWindow is None, "Requires the optional Tk desktop module")
class DesktopAuthorizationTests(unittest.TestCase):
    def window(self, app_id="123456"):
        return SimpleNamespace(
            _proc=None, _closing=False,
            app_id=Mock(get=Mock(return_value=app_id)),
            app_secret=Mock(get=Mock(return_value="secret")),
            model=Mock(get=Mock(return_value="model")),
            persona=Mock(get=Mock(return_value="persona")),
            pair_code=Mock(), pair_hint=Mock(), status=Mock(),
            _show_owner=Mock(), _append_log=Mock(), _set_controls=Mock(),
        )

    def test_saving_same_bot_preserves_latest_private_and_group_authorization(self):
        config = Config("123456", owner_id="owner", group_ids=["group"], private_user_ids=["friend"])
        with patch("desktop.load_config", return_value=config), patch("desktop.save_config") as save:
            self.assertTrue(BridgeWindow._save(self.window()))
        updated = save.call_args.args[0]
        self.assertEqual(updated.owner_id, "owner")
        self.assertEqual(updated.group_ids, ["group"])
        self.assertEqual(updated.private_user_ids, ["friend"])

    def test_changing_bot_clears_all_authorization(self):
        config = Config("123456", owner_id="owner", group_ids=["group"], private_user_ids=["friend"])
        with patch("desktop.load_config", return_value=config), patch("desktop.save_config") as save:
            self.assertTrue(BridgeWindow._save(self.window("654321")))
        updated = save.call_args.args[0]
        self.assertEqual(updated.owner_id, "")
        self.assertEqual(updated.group_ids, [])
        self.assertEqual(updated.private_user_ids, [])

    def test_unpairing_clears_private_users_and_group_bindings(self):
        config = Config("123456", owner_id="owner", group_ids=["group"], private_user_ids=["friend"])
        with patch("desktop.load_config", return_value=config), patch("desktop.save_config") as save:
            BridgeWindow._unpair(self.window())
        updated = save.call_args.args[0]
        self.assertEqual(updated.owner_id, "")
        self.assertEqual(updated.group_ids, [])
        self.assertEqual(updated.private_user_ids, [])
