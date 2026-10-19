"""Read timetable attendance and gate automatic number/radar submissions."""

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .config import get_cookies_path, load_config
from .utils import load_session


BASE_URL = "https://lnt.xmu.edu.cn"
REQUEST_TIMEOUT = (5, 12)
MIN_ATTENDEES = 10
MIN_PERCENT = 50
ATTENDED_STATUSES = {"on_call", "on_call_fine"}


class AttendanceDataError(ValueError):
    """The available data is insufficient to authorize a submission."""


@dataclass(frozen=True)
class AttendanceDecision:
    allowed: bool = False
    complete: bool = False
    message: str = ""
    number_code: str = ""


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AttendanceDataError("missing or invalid ID")
    if not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0:
        raise AttendanceDataError("missing or invalid ID")
    return str(int(value))


def _timestamp(value):
    if not isinstance(value, str):
        raise AttendanceDataError("invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AttendanceDataError("invalid timestamp") from None
    if result.tzinfo is None:
        raise AttendanceDataError("timestamp has no timezone")
    return result


def valid_number_code(value):
    # Preserve leading zeroes; do not select a code from an unrelated record.
    return isinstance(value, str) and re.fullmatch(r"[0-9]{4}", value) is not None


def _get_json(session, path, **kwargs):
    response = session.get(BASE_URL + path, timeout=REQUEST_TIMEOUT, **kwargs)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise AttendanceDataError("invalid response object")
    return data


class AttendanceGuard:
    def __init__(self, managed_account_ids):
        self.managed_account_ids = frozenset(_id(x) for x in managed_account_ids)
        self._student_ids = {}
        self._identity_lock = threading.Lock()

    @classmethod
    def for_accounts(cls, accounts):
        # Include complete configured accounts even when --account-id selects a subset.
        configured = load_config().get("accounts", [])
        managed = [a for a in configured if a.get("username") and a.get("password")]
        return cls([a.get("id") for a in managed + list(accounts)])

    def _identities(self, session, account):
        own_account_id = _id((account or {}).get("id"))
        if own_account_id not in self.managed_account_ids:
            raise AttendanceDataError("current account is not registered with the guard")
        # Cache verified identity mappings, never attendance counts or allow decisions.
        # A lock prevents simultaneous account threads from observing a partial set.
        with self._identity_lock:
            for account_id in sorted(self.managed_account_ids):
                if account_id in self._student_ids:
                    continue
                if account_id == own_account_id:
                    profile = _get_json(session, "/api/profile")
                else:
                    with requests.Session() as other_session:
                        if not load_session(other_session, get_cookies_path(account_id)):
                            raise AttendanceDataError(
                                f"managed account {account_id} has no cached session"
                            )
                        profile = _get_json(other_session, "/api/profile")
                self._student_ids[account_id] = _id(profile.get("id"))
            return frozenset(self._student_ids.values()), self._student_ids[own_account_id]

    def _record(self, session, rollcall, now):
        course_id = _id(rollcall.get("course_id"))
        rollcall_id = _id(rollcall.get("rollcall_id"))
        event_time = rollcall.get("rollcall_time") or rollcall.get("created_at")
        # The timetable endpoint keys rollcalls by the UTC calendar date of
        # rollcall_time, not the campus-local date, so derive the query date in UTC.
        if event_time:
            dates = [_timestamp(event_time).astimezone(timezone.utc).date()]
        else:
            today = now.astimezone(timezone.utc).date()
            # Discovery does not always include the start time. Exact ID matching
            # permits an active rollcall to survive the midnight (UTC) boundary.
            dates = [today, today - timedelta(days=1)]
        for day in dates:
            data = _get_json(session, "/api/timetable_rollcalls", params={
                "course_ids": course_id, "rollcall_date": day.isoformat(),
            })
            records = data.get("rollcalls")
            if not isinstance(records, list):
                raise AttendanceDataError("missing rollcalls list")
            if any(not isinstance(item, dict) for item in records):
                raise AttendanceDataError("invalid rollcall record")
            matches = [item for item in records if str(item.get("id")) == rollcall_id]
            if not matches:
                continue
            if len(matches) != 1:
                raise AttendanceDataError("ambiguous rollcall ID")
            record = matches[0]
            course = record.get("course")
            if not isinstance(course, dict) or _id(course.get("id")) != course_id:
                raise AttendanceDataError("course does not match the detected rollcall")
            record_time = record.get("rollcall_time") or record.get("created_at")
            if _timestamp(record_time).astimezone(timezone.utc).date() != day:
                raise AttendanceDataError("rollcall date does not match the request")
            return record
        raise AttendanceDataError("detected rollcall is absent from timetable results")

    def check(self, session, rollcall, account=None, now=None):
        try:
            return self._check(session, rollcall, account, now)
        except AttendanceDataError as exc:
            return AttendanceDecision(message=f"Waiting: {exc}.")
        except (requests.RequestException, ValueError, TypeError, OverflowError):
            # Do not log response bodies, codes, student lists, cookies or URLs.
            return AttendanceDecision(message="Waiting: attendance API or identity lookup failed.")

    def _check(self, session, rollcall, account, now):
        excluded, own_student_id = self._identities(session, account)
        record = self._record(session, rollcall, now or datetime.now(timezone.utc))
        # Identity lookups can take time. Check expiry against the time after all
        # network reads, rather than the beginning of the polling cycle.
        now = now or datetime.now(timezone.utc)
        if record.get("status") == "finished":
            return AttendanceDecision(complete=True, message="Rollcall has finished; skipped.")
        if record.get("status") != "in_progress":
            raise AttendanceDataError("rollcall is not confirmed in progress")
        if record.get("end_time") is not None and _timestamp(record["end_time"]) <= now:
            return AttendanceDecision(complete=True, message="Rollcall has expired; skipped.")

        is_number = not rollcall.get("is_radar") and rollcall.get("is_number")
        if not isinstance(record.get("is_number"), bool) or record["is_number"] != bool(is_number):
            raise AttendanceDataError("rollcall type does not match discovery")

        students = record.get("student_rollcalls")
        if not isinstance(students, list) or not students:
            raise AttendanceDataError("missing student attendance list")
        statuses = {}
        for student in students:
            if not isinstance(student, dict):
                raise AttendanceDataError("invalid student attendance record")
            student_id = _id(student.get("student_id"))
            status = student.get("status")
            if not isinstance(status, str) or not status.strip() or student_id in statuses:
                raise AttendanceDataError("missing status or duplicate student ID")
            statuses[student_id] = status
        if own_student_id not in statuses:
            raise AttendanceDataError("current student is absent from the roster")
        if statuses[own_student_id] in ATTENDED_STATUSES:
            return AttendanceDecision(complete=True, message="Already answered; skipped.")
        if statuses[own_student_id] != "absent":
            raise AttendanceDataError("current student is not confirmed absent")

        others = [status for student_id, status in statuses.items() if student_id not in excluded]
        total = len(others)
        attended = sum(status in ATTENDED_STATUSES for status in others)
        met = total > 0 and attended >= MIN_ATTENDEES and attended * 100 >= total * MIN_PERCENT
        summary = f"Other students: {attended}/{total}; require >= {MIN_PERCENT}% and >= {MIN_ATTENDEES}."
        if not met:
            return AttendanceDecision(message=f"Waiting. {summary}")
        number_code = record.get("number_code") if is_number else ""
        if is_number and not valid_number_code(number_code):
            return AttendanceDecision(message=f"Waiting: number code is unavailable. {summary}")
        return AttendanceDecision(allowed=True, message=f"Threshold met. {summary}", number_code=number_code)
