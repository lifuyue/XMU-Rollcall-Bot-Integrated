import json
import threading
import time
import uuid
from queue import Empty
from urllib.parse import parse_qs, urlparse

import requests

from .config import get_qr_config, load_config
from .notifier import build_notifier


base_url = "https://lnt.xmu.edu.cn"

mobile_headers = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Mobile Safari/537.36 Edg/141.0.0.0"
    ),
    "Content-Type": "application/json",
}

_server = None
_server_lock = threading.Lock()

# QR payload parser adapted from:
# https://github.com/KrsMt-0113/XMU-Rollcall-bot_qrCode/blob/main/qrRollcall/parse_code.py
_TA = chr(30)
_EA = chr(31)
_NA = chr(26)
_RA = chr(16)
_IA = _NA + "1"
_OA = _NA + "0"


def to_base36(num):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num < 0:
        return "-" + to_base36(-num)
    if num < 36:
        return chars[num]
    result = ""
    while num:
        num, rem = divmod(num, 36)
        result = chars[rem] + result
    return result


_AA = {
    key: to_base36(i)
    for i, key in enumerate(
        [
            "courseId",
            "activityId",
            "activityType",
            "data",
            "rollcallId",
            "groupSetId",
            "accessCode",
            "action",
            "enableGroupRollcall",
            "createUser",
            "joinCourse",
        ]
    )
}
_UA = {
    key: _NA + to_base36(i + 2)
    for i, key in enumerate(["classroom-exam", "feedback", "vote"])
}
_CA = {value: key for key, value in _AA.items()}
_SA = {value: key for key, value in _UA.items()}


class QRConfigError(RuntimeError):
    pass


class QRParseError(ValueError):
    pass


def parse_sign_qr_code(text):
    result = {}
    if not text or not isinstance(text, str):
        return result

    for part in filter(None, text.split("!")):
        splitted = part.split("~", 1)
        if len(splitted) < 2:
            continue
        raw_key, raw_value = splitted[0], splitted[1]
        key = _CA.get(raw_key, raw_key)

        if raw_value.startswith(_NA):
            if raw_value == _IA:
                value = True
            elif raw_value != _OA:
                value = _SA.get(raw_value, raw_value)
            else:
                value = False
        elif raw_value.startswith(_RA):
            parts = raw_value[1:].split(".")
            try:
                nums = [int(item, 36) for item in parts]
            except Exception:
                nums = []
            if len(nums) > 1:
                value = float(f"{nums[0]}.{nums[1]}")
            elif nums:
                value = nums[0]
            else:
                value = raw_value
        else:
            value = raw_value.replace(_EA, "~").replace(_TA, "!")

        result[key] = value
    return result


def parse_qr_code_text(text):
    if not text or not isinstance(text, str):
        raise QRParseError("empty QR code text")

    raw_text = text.strip()
    if raw_text.startswith("{"):
        data = json.loads(raw_text)
    else:
        data = _parse_qr_url_or_payload(raw_text)

    if not isinstance(data, dict) or not data:
        raise QRParseError("QR code payload is not a JSON object")
    if "rollcallId" not in data:
        raise QRParseError("QR code payload does not contain rollcallId")
    if "data" not in data:
        raise QRParseError("QR code payload does not contain data")
    return data


def _parse_qr_url_or_payload(text):
    candidate = text
    if candidate.startswith("/"):
        candidate = base_url + candidate
    elif "/j?p=" in candidate and not candidate.startswith("http"):
        candidate = base_url + candidate

    if not candidate.startswith("http"):
        parsed_payload = parse_sign_qr_code(candidate)
        if parsed_payload:
            return parsed_payload
        return json.loads(candidate)

    try:
        parsed_url = urlparse(candidate)
    except Exception as exc:
        raise QRParseError(f"invalid QR code URL: {exc}") from exc

    if parsed_url.path not in {"/j", "/scanner-jumper"}:
        raise QRParseError("unsupported QR code URL path")

    query = parse_qs(parsed_url.query)
    encoded_json = query.get("_p", [None])[0]
    if encoded_json:
        return json.loads(encoded_json)

    payload = query.get("p", [""])[0]
    parsed_payload = parse_sign_qr_code(payload)
    if not parsed_payload:
        raise QRParseError("failed to parse QR code payload")
    return parsed_payload


def handle_qr_rollcall(session, rollcall):
    config_data = load_config()
    qr_config = get_qr_config(config_data)
    notifier = build_notifier(config_data)

    if not qr_config.get("enabled"):
        print("[QR] QR rollcall detected, but qr.enabled is not true. Skipping QR flow.")
        notifier.send(
            "XMU QR Rollcall skipped",
            "QR rollcall was detected, but QR support is disabled in local config.",
        )
        return False

    if not qr_config.get("ngrok_token"):
        print("[QR] QR rollcall detected, but ngrok_token is missing. Skipping QR flow.")
        notifier.send(
            "XMU QR Rollcall skipped",
            "QR rollcall was detected, but qr.ngrok_token is missing.",
        )
        return False

    try:
        scan_session = _get_server(qr_config).create_session(qr_config["session_timeout"])
    except Exception as exc:
        print(f"[QR] Failed to start QR scanner service: {exc}")
        notifier.send("XMU QR scanner unavailable", str(exc))
        return False

    print(f"[QR] One-time scanner link: {scan_session.url}")
    notify_result = notifier.send_qr_link(
        rollcall,
        scan_session.url,
        scan_session.timeout,
    )
    if not notify_result.ok:
        print(f"[QR] Notification failed: {notify_result.message}")

    print(f"[QR] Waiting for scan result for up to {scan_session.timeout} seconds...")
    try:
        qr_text = scan_session.queue.get(timeout=scan_session.timeout + 5)
    except Empty:
        print("[QR] Timed out waiting for scan result.")
        return False

    if not qr_text:
        print("[QR] Scanner session expired before a QR code was submitted.")
        return False

    try:
        payload = parse_qr_code_text(qr_text)
    except Exception as exc:
        print(f"[QR] Failed to parse submitted QR code: {exc}")
        return False

    expected_id = str(rollcall.get("rollcall_id"))
    actual_id = str(payload.get("rollcallId"))
    if expected_id and actual_id != expected_id:
        print(f"[QR] Submitted QR rollcallId {actual_id} does not match current rollcall {expected_id}.")
        return False

    return submit_qr_rollcall(session, payload)


def submit_qr_rollcall(session, payload):
    rollcall_id = payload["rollcallId"]
    answer_url = f"{base_url}/api/rollcall/{rollcall_id}/answer_qr_rollcall"
    body = {
        "data": payload["data"],
        "deviceId": str(uuid.uuid4()),
    }
    started_at = time.time()

    try:
        response = session.put(answer_url, headers=mobile_headers, json=body)
    except requests.RequestException as exc:
        print(f"[QR] Failed to submit QR rollcall: {exc}")
        return False

    elapsed = time.time() - started_at
    if response.status_code == 200:
        print(f"[QR] QR rollcall answered successfully. Time: {elapsed:.2f} s.")
        return True

    print(f"[QR] QR rollcall submission failed. HTTP {response.status_code}. Time: {elapsed:.2f} s.")
    try:
        print(f"[QR] Server response: {response.json()}")
    except ValueError:
        print(f"[QR] Server response: {response.text[:300]}")
    return False


def _get_server(qr_config):
    global _server
    if _server is not None:
        _server.start()
        return _server

    with _server_lock:
        if _server is None:
            from .qr_server import QRServer

            _server = QRServer(
                port=qr_config["flask_port"],
                ngrok_token=qr_config["ngrok_token"],
            )
    _server.start()
    return _server
