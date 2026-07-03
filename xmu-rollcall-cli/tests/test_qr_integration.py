import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import quote

from xmu_rollcall.notifier import LogNotifier, build_notifier
from xmu_rollcall.qr_handler import parse_qr_code_text, parse_sign_qr_code


class QRParserTest(unittest.TestCase):
    def test_parse_compact_sign_payload(self):
        payload = parse_sign_qr_code("!4~123!3~dynamic-data!")

        self.assertEqual(payload["rollcallId"], "123")
        self.assertEqual(payload["data"], "dynamic-data")

    def test_parse_jumper_url_with_p_payload(self):
        compact = quote("!4~123!3~dynamic-data!")
        payload = parse_qr_code_text(f"https://lnt.xmu.edu.cn/j?p={compact}")

        self.assertEqual(payload["rollcallId"], "123")
        self.assertEqual(payload["data"], "dynamic-data")

    def test_parse_scanner_jumper_url_with_json_payload(self):
        encoded = quote(json.dumps({"rollcallId": "456", "data": "qr-token"}))
        payload = parse_qr_code_text(f"https://lnt.xmu.edu.cn/scanner-jumper?_p={encoded}")

        self.assertEqual(payload["rollcallId"], "456")
        self.assertEqual(payload["data"], "qr-token")


class NotifierConfigTest(unittest.TestCase):
    def test_telegram_missing_config_falls_back_to_log_notifier(self):
        with patch.dict(os.environ, {}, clear=True):
            notifier = build_notifier({"notification": {"provider": "telegram"}})

        self.assertIsInstance(notifier, LogNotifier)


if __name__ == "__main__":
    unittest.main()
