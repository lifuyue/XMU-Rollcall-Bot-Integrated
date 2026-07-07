import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

import requests

from .config import get_notification_config


class NotificationError(RuntimeError):
    """Notification send failed."""


@dataclass
class NotificationResult:
    ok: bool
    message: str = ""


class BaseNotifier:
    def send(self, title, message):
        raise NotImplementedError

    def _account_line(self, rollcall):
        account_name = rollcall.get("account_name")
        account_id = rollcall.get("account_id")
        if account_name and account_id is not None:
            return f"Account: {account_name} (ID: {account_id})\n"
        if account_name:
            return f"Account: {account_name}\n"
        return ""

    def send_rollcall_success(self, rollcall, rollcall_type):
        course = rollcall.get("course_title") or "Unknown course"
        department = rollcall.get("department_name") or "Unknown department"
        teacher = rollcall.get("created_by_name") or "Unknown teacher"
        rollcall_id = rollcall.get("rollcall_id") or "unknown"
        finished_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        message = (
            "XMU rollcall answered successfully\n"
            f"{self._account_line(rollcall)}"
            f"Type: {rollcall_type}\n"
            f"Course: {course}\n"
            f"Created by: {department} {teacher}\n"
            f"Rollcall ID: {rollcall_id}\n"
            f"Finished at: {finished_at}"
        )
        return self.send("XMU Rollcall Success", message)

    def send_qr_link(self, rollcall, scan_url, timeout_seconds):
        course = rollcall.get("course_title") or "Unknown course"
        department = rollcall.get("department_name") or "Unknown department"
        teacher = rollcall.get("created_by_name") or "Unknown teacher"
        expires_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() + int(timeout_seconds)),
        )
        message = (
            "XMU QR rollcall detected\n"
            f"{self._account_line(rollcall)}"
            f"Course: {course}\n"
            f"Created by: {department} {teacher}\n"
            f"Scan link: {scan_url}\n"
            f"Valid for: {timeout_seconds} seconds\n"
            f"Expires at: {expires_at}"
        )
        return self.send("XMU QR Rollcall", message)


class LogNotifier(BaseNotifier):
    def send(self, title, message):
        print(f"[Notification] {title}\n{message}")
        return NotificationResult(ok=True, message="logged")


class TelegramNotifier(BaseNotifier):
    api_base = "https://api.telegram.org"

    def __init__(self, bot_token, chat_id, timeout=10):
        if not bot_token:
            raise NotificationError("telegram_bot_token is required")
        if not chat_id:
            raise NotificationError("telegram_chat_id is required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, title, message):
        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        text = f"{title}\n\n{message}"
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            print(f"[Notification] Telegram request failed: {exc}")
            return NotificationResult(ok=False, message=str(exc))

        if response.status_code != 200:
            print(f"[Notification] Telegram returned HTTP {response.status_code}.")
            return NotificationResult(ok=False, message=f"http {response.status_code}")
        return NotificationResult(ok=True, message="sent")


class FeishuNotifier(BaseNotifier):
    def __init__(self, webhook_url, sign_secret="", timeout=10):
        if not webhook_url:
            raise NotificationError("feishu_webhook_url is required")
        self.webhook_url = webhook_url
        self.sign_secret = sign_secret or ""
        self.timeout = timeout

    def send(self, title, message):
        payload = self._build_text_payload(f"{title}\n\n{message}")
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            print(f"[Notification] Feishu request failed: {exc}")
            return NotificationResult(ok=False, message=str(exc))

        if response.status_code != 200:
            print(f"[Notification] Feishu returned HTTP {response.status_code}.")
            return NotificationResult(ok=False, message=f"http {response.status_code}")

        try:
            data = response.json()
        except ValueError:
            data = {}

        error_message = self._response_error(data)
        if error_message:
            print(f"[Notification] Feishu returned error: {error_message}")
            return NotificationResult(ok=False, message=error_message)
        return NotificationResult(ok=True, message="sent")

    def _build_text_payload(self, text):
        payload = {
            "msg_type": "text",
            "content": {
                "text": text,
            },
        }
        if self.sign_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._make_signature(timestamp)
        return payload

    def _make_signature(self, timestamp):
        string_to_sign = f"{timestamp}\n{self.sign_secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _response_error(self, data):
        if not isinstance(data, dict):
            return ""

        if "code" in data and str(data.get("code")) != "0":
            return data.get("msg") or data.get("message") or f"code {data.get('code')}"

        if "StatusCode" in data and str(data.get("StatusCode")) != "0":
            return (
                data.get("StatusMessage")
                or data.get("msg")
                or f"StatusCode {data.get('StatusCode')}"
            )

        return ""


def build_notifier(config=None):
    notification_config = get_notification_config(config)
    provider = notification_config.get("provider", "log")

    if provider == "telegram":
        try:
            return TelegramNotifier(
                notification_config.get("telegram_bot_token"),
                notification_config.get("telegram_chat_id"),
            )
        except NotificationError as exc:
            print(f"[Notification] Telegram disabled: {exc}. Falling back to log output.")
            return LogNotifier()

    if provider in {"feishu", "lark"}:
        try:
            return FeishuNotifier(
                notification_config.get("feishu_webhook_url"),
                notification_config.get("feishu_sign_secret"),
                notification_config.get("feishu", {}).get("timeout", 10),
            )
        except NotificationError as exc:
            print(f"[Notification] Feishu disabled: {exc}. Falling back to log output.")
            return LogNotifier()

    if provider == "wechat":
        try:
            from .wechat_notifier import WeChatNotifier

            return WeChatNotifier(notification_config.get("wechat"))
        except NotificationError as exc:
            print(f"[Notification] WeChat disabled: {exc}. Falling back to log output.")
            return LogNotifier()

    if provider not in {"log", "none", ""}:
        print(f"[Notification] Unknown provider '{provider}'. Falling back to log output.")
    return LogNotifier()
