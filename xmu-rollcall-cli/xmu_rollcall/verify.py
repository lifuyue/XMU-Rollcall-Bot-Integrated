import uuid
import math
import json
import threading
import time
import requests
from datetime import datetime, timezone
from urllib3.util import Timeout

from .attendance_guard import REQUEST_TIMEOUT, valid_number_code, _id, _timestamp

base_url = "https://lnt.xmu.edu.cn"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://ids.xmu.edu.cn/authserver/login",
}

# (latitude, longitude), in fallback order; retain the supplied four decimal places.
RADAR_FALLBACK_LOCATIONS = (
    (24.6082, 118.2953),
    (24.6087, 118.3090),
    (24.6075, 118.2950),  # 24°36′27″ N, 118°17′42″ E.
)
DISTANCE_SECONDS = 10.0

def send_code(in_session, rollcall_id, number_code):
    """Submit only the exact code selected by the attendance guard."""
    if not valid_number_code(number_code):
        print("Number code is unavailable; no answer submitted.")
        return False
    answer_url = f"{base_url}/api/rollcall/{rollcall_id}/answer_number_rollcall"
    payload = {"deviceId": str(uuid.uuid4()), "numberCode": number_code}
    try:
        response = in_session.put(
            answer_url, json=payload, headers=in_session.headers, timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            print("Number code rollcall answered successfully.")
            return True
        print(f"Failed to submit number code. Status: {response.status_code}")
    except requests.RequestException:
        print("Failed to submit number code: request failed or timed out.")
    return False


class _RadarStopped(Exception):
    def __init__(self, reason, success=False):
        self.reason = reason
        self.success = success


class _RadarFallback(Exception):
    """Pass normal distance failure and updated context to the stage supervisor."""
    def __init__(self, reason, end_time):
        self.reason = reason
        self.end_time = end_time


class _RadarRun:
    def __init__(self, session, rollcall_id, student_id, course_id, end_time, deadline, diagnostic):
        self.session = session
        self.rollcall_id = rollcall_id
        self.student_id = student_id
        self.course_id = course_id
        self.end_time = end_time
        self.deadline = deadline
        self.distance_deadline = None
        self.diagnostic = diagnostic

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.diagnostic["phase"] = "budget"
            raise _RadarStopped("budget_exhausted")
        if self.end_time is not None:
            until_end = (_timestamp(self.end_time) - datetime.now(timezone.utc)).total_seconds()
            if until_end <= 0:
                raise _RadarStopped("rollcall_ended")
            remaining = min(remaining, until_end)
        if self.distance_deadline is not None:
            until_distance = self.distance_deadline - time.monotonic()
            if until_distance <= 0:
                self.diagnostic["phase"] = "budget"
                raise _RadarStopped("distance_timeout")
            remaining = min(remaining, until_distance)
        return remaining

    def timeout(self):
        remaining = self.remaining()
        # The parent process enforces the wall-clock deadline, including slow reads/DNS.
        return Timeout(total=remaining, connect=min(REQUEST_TIMEOUT[0], remaining),
                       read=min(REQUEST_TIMEOUT[1], remaining))

    def check_status(self):
        self.remaining()
        self.diagnostic.update(phase="status_request", http_status=None)
        try:
            response = self.session.get(
                f"{base_url}/api/rollcall/{self.rollcall_id}",
                headers=headers, timeout=self.timeout(), allow_redirects=False,
            )
            self.diagnostic.update(phase="status_response", http_status=response.status_code)
            _radar_log(self.diagnostic, "status_response")
            self.remaining()
            if response.status_code != 200:
                raise _RadarStopped("status_check_failed")
            self.diagnostic["phase"] = "status_json"
            record = response.json()
            self.diagnostic["phase"] = "status_validation"
            if (not isinstance(record, dict) or _id(record.get("id")) != _id(self.rollcall_id)
                    or _id(record.get("course_id")) != _id(self.course_id)
                    or record.get("is_radar") is not True):
                raise _RadarStopped("status_validation_failed")
            if record.get("status") == "finished":
                raise _RadarStopped("rollcall_ended")
            if record.get("status") != "in_progress":
                raise _RadarStopped("status_validation_failed")
            # Preserve any known end time if a later response omits it.
            if record.get("end_time") is not None:
                self.end_time = record["end_time"]
            self.remaining()
            rows = record.get("student_rollcalls")
            if not isinstance(rows, list):
                raise _RadarStopped("status_validation_failed")
            own = [row for row in rows if isinstance(row, dict)
                   and _id(row.get("student_id")) == _id(self.student_id)]
            if len(own) != 1:
                raise _RadarStopped("status_validation_failed")
            if own[0].get("status") in {"on_call", "on_call_fine"}:
                raise _RadarStopped("already_answered", success=True)
            if own[0].get("status") != "absent":
                raise _RadarStopped("student_not_absent")
        except (requests.RequestException, ValueError, TypeError, OverflowError) as exc:
            _radar_exception(self.diagnostic, exc)
            self.remaining()
            raise _RadarStopped("status_check_failed") from None


def _radar_locations():
    """Three centers first, then each center's E/S/W/N points at 100 metres."""
    for index, (lat, lon) in enumerate(RADAR_FALLBACK_LOCATIONS, 1):
        yield f"fixed_{index}", lat, lon
    angular_distance = 100.0 / 6371000.0
    for index, (lat, lon) in enumerate(RADAR_FALLBACK_LOCATIONS, 1):
        phi, lam = math.radians(lat), math.radians(lon)
        for direction, bearing in (("east", 90), ("south", 180), ("west", 270), ("north", 0)):
            theta = math.radians(bearing)
            phi2 = math.asin(math.sin(phi) * math.cos(angular_distance)
                             + math.cos(phi) * math.sin(angular_distance) * math.cos(theta))
            lam2 = lam + math.atan2(math.sin(theta) * math.sin(angular_distance) * math.cos(phi),
                                    math.cos(angular_distance) - math.sin(phi) * math.sin(phi2))
            # Keep full computed precision; rounding to four decimals shifts a 100m point.
            yield f"fixed_{index}_{direction}", math.degrees(phi2), (math.degrees(lam2) + 180) % 360 - 180


def send_radar(in_session, rollcall_id, *, student_id=None, course_id=None, end_time=None, deadline=None,
               stage="all", fallback_reason="distance_method_failed"):
    """Run one distance attempt and up to two search rounds within a shared deadline.

    Production callers use radar_campaign to persist the claim and enforce the
    distance and total deadlines in terminable child processes. Direct callers
    check before/after requests, but requests alone cannot bound wall-clock time.
    A distance-only stage raises _RadarFallback when the supervisor should advance.
    """
    diagnostic = {
        "rollcall_id": rollcall_id,
        "worker": threading.current_thread().name,
        "attempt": "probe_1", "phase": "request", "http_status": None,
    }
    run = _RadarRun(in_session, rollcall_id, student_id, course_id, end_time,
                    time.monotonic() + 60 if deadline is None else deadline, diagnostic)
    try:
        if stage not in {"all", "distance", "fallback"}:
            raise ValueError("invalid radar stage")
        # Missing identity must never allow an unchecked submission.
        _id(student_id)
        _id(course_id)
        _id(rollcall_id)
        run.remaining()
        if stage != "fallback":
            run.distance_deadline = min(run.deadline, time.monotonic() + DISTANCE_SECONDS)
            try:
                if _send_radar(in_session, rollcall_id, diagnostic, run):
                    return True
            except _RadarStopped as stopped:
                if stopped.reason != "distance_timeout" and not (
                        stage == "distance" and stopped.reason == "budget_exhausted"):
                    raise
                fallback_reason = "distance_timeout"
                _radar_log(diagnostic, "distance_timeout", reason=fallback_reason)
            except (requests.RequestException, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
                _radar_exception(diagnostic, exc)
            finally:
                run.distance_deadline = None
            if stage == "distance":
                raise _RadarFallback(fallback_reason, run.end_time)

        run.remaining()
        diagnostic.update(attempt="fixed_1", phase="request", http_status=None)
        diagnostic.pop("json_type", None)
        diagnostic.pop("distance_type", None)
        _radar_log(diagnostic, "fallback_start", reason=fallback_reason)
        for round_number in (1, 2):
            if round_number == 2:
                diagnostic["phase"] = "waiting"
                time.sleep(min(1.0, run.remaining()))
            run.remaining()
            diagnostic.update(round=round_number, attempt="fixed_1", phase="request", http_status=None)
            _radar_log(diagnostic, "round_start")
            for attempt, latitude, longitude in _radar_locations():
                try:
                    response = _radar_submit(in_session, rollcall_id, diagnostic,
                                             _radar_payload(latitude, longitude), attempt, run)
                    if response.status_code == 200:
                        _radar_log(diagnostic, "success")
                        return True
                except (requests.RequestException, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
                    _radar_exception(diagnostic, exc)
            _radar_log(diagnostic, "round_end")
        _radar_log(diagnostic, "failed", reason="all_fixed_locations_rejected")
    except _RadarStopped as stopped:
        _radar_log(diagnostic, "stopped", reason=stopped.reason)
        return stopped.success
    except (ValueError, TypeError, OverflowError) as exc:
        _radar_exception(diagnostic, exc)
    return False


def _radar_log(diagnostic, event, **details):
    print("[Radar] " + json.dumps({**diagnostic, "event": event, **details}, ensure_ascii=True), flush=True)


def _radar_exception(diagnostic, exc):
    reason = {
        "request": "request_failed", "json": "json_decode_failed",
        "distance": "distance_validation_failed", "geometry": "geometry_failed",
        "status_request": "status_request_failed", "status_json": "status_json_failed",
        "status_validation": "status_validation_failed",
    }.get(diagnostic["phase"], "radar_failed")
    # Request/JSON exceptions can contain URLs, tokens or response bodies.
    if type(exc) is ValueError and exc.args in (
        ("missing radar distance",), ("invalid radar distance",),
    ):
        reason = exc.args[0]
    details = {"exception_type": type(exc).__name__, "reason": reason}
    if isinstance(exc, (json.JSONDecodeError, requests.exceptions.JSONDecodeError)):
        details.update(json_line=exc.lineno, json_column=exc.colno)
    _radar_log(diagnostic, "exception", **details)
    print("Failed to submit radar answer: invalid response or request failure.")


def _radar_payload(lat, lon):
    return {
        "accuracy": 35,
        "altitude": 0,
        "altitudeAccuracy": None,
        "deviceId": str(uuid.uuid4()),
        "heading": None,
        "latitude": lat,
        "longitude": lon,
        "speed": None,
    }


def _radar_submit(in_session, rollcall_id, diagnostic, data, attempt, run):
    diagnostic.update(attempt=attempt, phase="request", http_status=None)
    diagnostic.pop("json_type", None)
    diagnostic.pop("distance_type", None)
    run.check_status()
    diagnostic.update(phase="request", http_status=None)
    response = in_session.put(
        f"{base_url}/api/rollcall/{rollcall_id}/answer",
        json=data, headers=headers, timeout=run.timeout(), allow_redirects=False,
    )
    diagnostic.update(phase="response", http_status=response.status_code)
    _radar_log(diagnostic, "http_response")
    run.remaining()
    if response.status_code in (401, 429):
        raise _RadarStopped("authentication_failed" if response.status_code == 401 else "rate_limited")
    return response


def _send_radar(in_session, rollcall_id, diagnostic, run):
    lat_1, lat_2 = 24.3, 24.6
    lon_1, lon_2 = 118.0, 118.2

    def submit(data, attempt):
        return _radar_submit(in_session, rollcall_id, diagnostic, data, attempt, run)

    def read_distance(response):
        diagnostic["phase"] = "json"
        data = response.json()
        diagnostic.update(
            phase="distance", json_type=type(data).__name__,
            distance_type=(type(data["distance"]).__name__ if "distance" in data else "missing")
            if isinstance(data, dict) else "unavailable",
        )
        return _distance(data)

    res_1 = submit(_radar_payload(lat_1, lon_1), "probe_1")
    if res_1.status_code == 200:
        return True
    distance_1 = read_distance(res_1)

    res_2 = submit(_radar_payload(lat_2, lon_2), "probe_2")
    if res_2.status_code == 200:
        return True
    distance_2 = read_distance(res_2)

    def latlon_to_xy(lat, lon, lat0, lon0):
        R = 6371000
        x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
        y = math.radians(lat - lat0) * R
        return x, y

    def xy_to_latlon(x, y, lat0, lon0):
        R = 6371000
        lat = lat0 + math.degrees(y / R)
        lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
        return lat, lon

    def circle_intersections(x1, y1, d1, x2, y2, d2):
        D = math.hypot(x2 - x1, y2 - y1)

        if D == 0 or D > d1 + d2 or D < abs(d1 - d2):
            return None

        a = (d1**2 - d2**2 + D**2) / (2 * D)
        h = math.sqrt(max(0.0, d1**2 - a**2))

        xm = x1 + a * (x2 - x1) / D
        ym = y1 + a * (y2 - y1) / D

        rx = -(y2 - y1) * (h / D)
        ry = (x2 - x1) * (h / D)

        p1 = (xm + rx, ym + ry)
        p2 = (xm - rx, ym - ry)
        return p1, p2

    def solve_two_points(lat1, lon1, lat2, lon2, d1, d2):
        lat0 = (lat1 + lat2) / 2
        lon0 = (lon1 + lon2) / 2
        x1, y1 = latlon_to_xy(lat1, lon1, lat0, lon0)
        x2, y2 = latlon_to_xy(lat2, lon2, lat0, lon0)

        sols = circle_intersections(x1, y1, d1, x2, y2, d2)
        if sols is None:
            return None

        p1 = xy_to_latlon(sols[0][0], sols[0][1], lat0, lon0)
        p2 = xy_to_latlon(sols[1][0], sols[1][1], lat0, lon0)
        return p1, p2

    diagnostic.update(attempt="solve", phase="geometry", http_status=None)
    diagnostic.pop("json_type", None)
    diagnostic.pop("distance_type", None)
    resolutions = solve_two_points(lat_1, lon_1, lat_2, lon_2, distance_1, distance_2)
    if resolutions:
        ((sol_x_1, sol_y_1), (sol_x_2, sol_y_2)) = resolutions
    else:
        _radar_log(diagnostic, "failed", reason="no_circle_intersection")
        return False

    payload_1 = _radar_payload(sol_x_1, sol_y_1)
    payload_2 = _radar_payload(sol_x_2, sol_y_2)

    res_3 = submit(payload_1, "candidate_1")
    if res_3.status_code == 200:
        return True
    else:
        res_4 = submit(payload_2, "candidate_2")
        if res_4.status_code == 200:
            return True

    _radar_log(diagnostic, "failed", reason="all_candidates_rejected")
    return False


def _distance(data):
    value = data.get("distance") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing radar distance")
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid radar distance")
    return value
