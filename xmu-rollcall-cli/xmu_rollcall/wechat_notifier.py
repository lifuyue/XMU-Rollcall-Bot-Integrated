import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .notifier import BaseNotifier, LogNotifier, NotificationError, NotificationResult


DEFAULT_WECHAT_CONFIG = {
    "enabled": False,
    "cred_path": str(CONFIG_DIR / "wechatbot-credentials.json"),
    "state_path": str(CONFIG_DIR / "wechat_state.json"),
    "send_timeout": 15,
    "bind_timeout": 300,
    "bot_agent": "XMU-Rollcall-Bot/0.1",
}


def load_wechat_state(state_path):
    path = Path(state_path).expanduser()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[Notification] WeChat state file is unreadable: {exc}")
        return {}

    if not isinstance(state, dict):
        return {}
    return {
        "target_user_id": state.get("target_user_id") or "",
        "context_token": state.get("context_token") or "",
        "updated_at": state.get("updated_at") or "",
    }


def save_wechat_state(state_path, state):
    path = Path(state_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "target_user_id": state.get("target_user_id") or "",
        "context_token": state.get("context_token") or "",
        "updated_at": state.get("updated_at") or _utc_now(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return payload


class WeChatNotifier(BaseNotifier):
    def __init__(self, wechat_config=None, fallback_notifier=None):
        self.config = _normalize_wechat_config(wechat_config)
        self.fallback_notifier = fallback_notifier or LogNotifier()

    def send(self, title, message):
        if not self.config.get("enabled"):
            return self._fallback(title, message, "wechat.enabled is not true")

        state = load_wechat_state(self.config["state_path"])
        target_user_id = state.get("target_user_id")
        context_token = state.get("context_token")
        if not target_user_id or not context_token:
            return self._fallback(
                title,
                message,
                "target group is not bound; run `xmu wechat-bind` first",
            )

        if not Path(self.config["cred_path"]).expanduser().exists():
            return self._fallback(
                title,
                message,
                "credentials are missing; run `xmu wechat-login` first",
            )

        text = f"{title}\n\n{message}"
        try:
            _run_async(lambda: self._send_async(target_user_id, context_token, text))
        except Exception as exc:
            return self._fallback(title, message, f"send failed: {exc}")

        return NotificationResult(ok=True, message="sent")

    async def _send_async(self, target_user_id, context_token, text):
        bot = _make_wechat_bot(self.config)
        _restore_context_token(bot, target_user_id, context_token)
        await bot.login()
        _restore_context_token(bot, target_user_id, context_token)
        await asyncio.wait_for(
            bot.send(target_user_id, text),
            timeout=int(self.config["send_timeout"]),
        )

    def _fallback(self, title, message, reason):
        print(f"[Notification] WeChat unavailable: {reason}. Falling back to log output.")
        result = self.fallback_notifier.send(title, message)
        return NotificationResult(
            ok=result.ok,
            message=f"wechat fallback: {reason}",
        )


def run_wechat_login(wechat_config=None, force=False):
    return _run_async(lambda: _wechat_login_async(wechat_config, force=force))


def run_wechat_bind(wechat_config=None):
    return _run_async(lambda: _wechat_bind_async(wechat_config))


def run_wechat_test(wechat_config=None, text=None):
    config = _normalize_wechat_config(wechat_config)
    notifier = WeChatNotifier(config)
    message = text or (
        "This is a test message from XMU Rollcall Bot.\n"
        f"Sent at: {_local_now()}"
    )
    return notifier.send("XMU WeChat Notification Test", message)


async def _wechat_login_async(wechat_config=None, force=False):
    config = _normalize_wechat_config(wechat_config)
    bot = _make_wechat_bot(config, print_callbacks=True)
    creds = await bot.login(force=force)
    print(f"[WeChat] Login succeeded. Credentials saved at: {config['cred_path']}", flush=True)
    return creds


async def _wechat_bind_async(wechat_config=None):
    config = _normalize_wechat_config(wechat_config)
    timeout = int(config["bind_timeout"])
    state_path = config["state_path"]
    bot = _make_wechat_bot(config, print_callbacks=True)
    bound_state = {}

    @bot.on_message
    async def handle_message(msg):
        state = _state_from_message(bot, msg)
        if not state:
            print("[WeChat] Received a message, but no usable context_token was present.", flush=True)
            return

        saved = save_wechat_state(state_path, state)
        _restore_context_token(bot, saved["target_user_id"], saved["context_token"])
        bound_state.update(saved)
        print(
            f"[WeChat] Bound target user_id: {saved['target_user_id']}. "
            f"State saved at: {state_path}",
            flush=True,
        )
        bot.stop()

    await bot.login()
    _restore_state_context(bot, state_path)
    print(
        "[WeChat] Waiting for one message from the target chat. "
        f"Timeout: {timeout} seconds.",
        flush=True,
    )

    try:
        await asyncio.wait_for(bot.start(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        bot.stop()
        raise NotificationError(
            "Timed out waiting for a WeChat message from the target chat."
        ) from exc

    if not bound_state:
        raise NotificationError("WeChat bind stopped before any target chat was saved.")
    return bound_state


def _normalize_wechat_config(wechat_config=None):
    config = dict(DEFAULT_WECHAT_CONFIG)
    config.update(dict(wechat_config or {}))
    config["enabled"] = _as_bool(config.get("enabled"))
    config["cred_path"] = str(Path(config["cred_path"]).expanduser())
    config["state_path"] = str(Path(config["state_path"]).expanduser())
    config["send_timeout"] = _positive_int(config.get("send_timeout"), 15)
    config["bind_timeout"] = _positive_int(config.get("bind_timeout"), 300)
    config["bot_agent"] = str(config.get("bot_agent") or DEFAULT_WECHAT_CONFIG["bot_agent"])
    return config


def _make_wechat_bot(config, print_callbacks=False):
    WeChatBot = _load_wechatbot_class()

    callbacks = {}
    if print_callbacks:
        callbacks = {
            "on_qr_url": lambda url: print(f"[WeChat] Scan this login URL in WeChat: {url}", flush=True),
            "on_scanned": lambda: print("[WeChat] QR scanned. Confirm login in WeChat.", flush=True),
            "on_expired": lambda: print("[WeChat] QR code expired. Waiting for a new one.", flush=True),
            "on_error": lambda err: print(f"[WeChat] Error: {err}", flush=True),
            "on_verify_code": _prompt_verify_code,
        }

    return WeChatBot(
        cred_path=config["cred_path"],
        bot_agent=config["bot_agent"],
        **callbacks,
    )


def _load_wechatbot_class():
    try:
        from wechatbot import WeChatBot
    except ImportError as exc:
        raise NotificationError(
            "wechatbot-sdk is not installed. Run `pip install -e ./xmu-rollcall-cli` in this project."
        ) from exc
    return WeChatBot


def _prompt_verify_code(is_retry):
    prompt = (
        "[WeChat] Pairing code mismatch. Enter the code shown in WeChat again: "
        if is_retry
        else "[WeChat] Enter the pairing code shown in WeChat: "
    )
    return input(prompt).strip()


def _restore_state_context(bot, state_path):
    state = load_wechat_state(state_path)
    user_id = state.get("target_user_id")
    context_token = state.get("context_token")
    if user_id and context_token:
        _restore_context_token(bot, user_id, context_token)
    return state


def _restore_context_token(bot, user_id, context_token):
    context_tokens = getattr(bot, "_context_tokens", None)
    if isinstance(context_tokens, dict) and user_id and context_token:
        context_tokens[user_id] = context_token


def _state_from_message(bot, msg):
    user_id = getattr(msg, "user_id", "") or ""
    context_token = getattr(msg, "_context_token", "") or ""
    if not context_token:
        context_tokens = getattr(bot, "_context_tokens", None)
        if isinstance(context_tokens, dict):
            context_token = context_tokens.get(user_id, "")

    if not user_id or not context_token:
        return {}

    return {
        "target_user_id": user_id,
        "context_token": context_token,
        "updated_at": _utc_now(),
    }


def _run_async(coro_factory):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    raise RuntimeError("WeChat notifier cannot run inside an existing asyncio event loop.")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _local_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
