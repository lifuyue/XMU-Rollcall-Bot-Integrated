import base64
import hashlib
import hmac
import os
import unittest
from unittest.mock import MagicMock, patch

from xmu_rollcall.config import get_notification_config
from xmu_rollcall.notifier import FeishuNotifier, LogNotifier, build_notifier


class FeishuNotifierTest(unittest.TestCase):
    def test_feishu_notification_config_is_parsed(self):
        config = {
            "notification": {
                "provider": "feishu",
                "feishu": {
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                    "sign_secret": "secret",
                    "timeout": "20",
                },
            }
        }

        with patch.dict(os.environ, {}, clear=True):
            notification = get_notification_config(config)

        self.assertEqual(notification["provider"], "feishu")
        self.assertEqual(notification["feishu_webhook_url"], config["notification"]["feishu"]["webhook_url"])
        self.assertEqual(notification["feishu_sign_secret"], "secret")
        self.assertEqual(notification["feishu"]["timeout"], 20)

    def test_feishu_environment_overrides_config(self):
        config = {
            "notification": {
                "provider": "log",
                "feishu_webhook_url": "config-webhook",
                "feishu_sign_secret": "config-secret",
            }
        }

        with patch.dict(
            os.environ,
            {
                "XMU_ROLLCALL_NOTIFICATION_PROVIDER": "feishu",
                "XMU_ROLLCALL_FEISHU_WEBHOOK_URL": "env-webhook",
                "XMU_ROLLCALL_FEISHU_SIGN_SECRET": "env-secret",
                "XMU_ROLLCALL_FEISHU_TIMEOUT": "30",
            },
            clear=True,
        ):
            notification = get_notification_config(config)

        self.assertEqual(notification["provider"], "feishu")
        self.assertEqual(notification["feishu_webhook_url"], "env-webhook")
        self.assertEqual(notification["feishu_sign_secret"], "env-secret")
        self.assertEqual(notification["feishu"]["timeout"], 30)

    def test_build_notifier_returns_feishu_notifier_for_feishu_provider(self):
        notifier = build_notifier(
            {
                "notification": {
                    "provider": "feishu",
                    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                }
            }
        )

        self.assertIsInstance(notifier, FeishuNotifier)

    def test_feishu_missing_webhook_falls_back_to_log_notifier(self):
        notifier = build_notifier({"notification": {"provider": "feishu"}})

        self.assertIsInstance(notifier, LogNotifier)

    def test_feishu_send_posts_text_payload(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 0, "msg": "success"}

        with patch("xmu_rollcall.notifier.requests.post", return_value=response) as post:
            result = FeishuNotifier("https://example.test/webhook").send("Title", "Message")

        self.assertTrue(result.ok)
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"]["text"], "Title\n\nMessage")
        self.assertNotIn("timestamp", payload)
        self.assertNotIn("sign", payload)

    def test_feishu_send_includes_signature_when_secret_configured(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"StatusCode": 0, "StatusMessage": "success"}
        timestamp = "1700000000"
        secret = "sign-secret"
        expected_sign = base64.b64encode(
            hmac.new(
                f"{timestamp}\n{secret}".encode("utf-8"),
                b"",
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        with (
            patch("xmu_rollcall.notifier.time.time", return_value=int(timestamp)),
            patch("xmu_rollcall.notifier.requests.post", return_value=response) as post,
        ):
            result = FeishuNotifier("https://example.test/webhook", secret).send("Title", "Message")

        self.assertTrue(result.ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["timestamp"], timestamp)
        self.assertEqual(payload["sign"], expected_sign)

    def test_feishu_api_error_returns_failure(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 19024, "msg": "invalid sign"}

        with patch("xmu_rollcall.notifier.requests.post", return_value=response):
            result = FeishuNotifier("https://example.test/webhook").send("Title", "Message")

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "invalid sign")


if __name__ == "__main__":
    unittest.main()
