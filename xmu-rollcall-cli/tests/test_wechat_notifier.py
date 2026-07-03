import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xmu_rollcall.config import get_notification_config
from xmu_rollcall.notifier import build_notifier
from xmu_rollcall.wechat_notifier import (
    WeChatNotifier,
    load_wechat_state,
    save_wechat_state,
)


class WeChatNotifierTest(unittest.TestCase):
    def test_wechat_notification_config_is_parsed(self):
        config = {
            "notification": {
                "provider": "wechat",
                "wechat": {
                    "enabled": True,
                    "cred_path": "~/wechat-cred.json",
                    "state_path": "~/wechat-state.json",
                    "send_timeout": "20",
                    "bind_timeout": "120",
                    "bot_agent": "TestAgent/1.0",
                },
            }
        }

        with patch.dict(os.environ, {}, clear=True):
            notification = get_notification_config(config)

        self.assertEqual(notification["provider"], "wechat")
        self.assertTrue(notification["wechat"]["enabled"])
        self.assertEqual(notification["wechat"]["send_timeout"], 20)
        self.assertEqual(notification["wechat"]["bind_timeout"], 120)
        self.assertEqual(notification["wechat"]["bot_agent"], "TestAgent/1.0")
        self.assertTrue(notification["wechat"]["cred_path"].endswith("wechat-cred.json"))
        self.assertTrue(notification["wechat"]["state_path"].endswith("wechat-state.json"))

    def test_build_notifier_returns_wechat_notifier_for_wechat_provider(self):
        notifier = build_notifier(
            {
                "notification": {
                    "provider": "wechat",
                    "wechat": {"enabled": False},
                }
            }
        )

        self.assertIsInstance(notifier, WeChatNotifier)

    def test_wechat_notifier_falls_back_to_log_when_unbound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notifier = WeChatNotifier(
                {
                    "enabled": True,
                    "cred_path": str(Path(temp_dir) / "credentials.json"),
                    "state_path": str(Path(temp_dir) / "wechat_state.json"),
                }
            )

            with patch("builtins.print"):
                result = notifier.send("Title", "Message")

        self.assertTrue(result.ok)
        self.assertIn("target group is not bound", result.message)

    def test_wechat_state_saves_only_target_context_and_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "wechat_state.json"

            saved = save_wechat_state(
                state_path,
                {
                    "target_user_id": "group-id",
                    "context_token": "context-token",
                    "text": "do not save message content",
                },
            )
            loaded = load_wechat_state(state_path)

        self.assertEqual(set(saved.keys()), {"target_user_id", "context_token", "updated_at"})
        self.assertEqual(loaded["target_user_id"], "group-id")
        self.assertEqual(loaded["context_token"], "context-token")
        self.assertTrue(loaded["updated_at"])


if __name__ == "__main__":
    unittest.main()
