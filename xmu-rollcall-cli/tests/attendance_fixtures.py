"""Synthetic fixtures only; never use account cookies or contact remote services."""

from unittest.mock import MagicMock


ACCOUNT = {"id": 1, "name": "Test", "username": "test", "password": "test"}


def detected(**overrides):
    item = {
        "course_title": "Test course", "course_id": 42,
        "created_by_name": "Teacher", "department_name": "Department",
        "is_expired": False, "is_number": True, "is_radar": False,
        "rollcall_id": 123, "rollcall_status": "in_progress",
        "scored": False, "status": "absent", "created_at": "2026-10-13T02:00:00Z",
    }
    item.update(overrides)
    return item


def timetable(attended=10, total=20, managed=None, **overrides):
    students = [{"student_id": 900, "status": "absent"}] if managed is None else managed
    item = {
        "id": 123, "course": {"id": 42, "name": "Test course"},
        "created_at": "2026-10-13T02:00:00Z", "rollcall_time": "2026-10-13T02:00:00Z",
        "end_time": None, "is_number": True, "status": "in_progress",
        "number_code": "0034", "type": "another",
        "student_rollcalls": students + [
            {"student_id": i + 1, "status": "on_call" if i < attended else "absent"}
            for i in range(total)
        ],
    }
    item.update(overrides)
    return item


def response(data, status=200):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = data
    return result


def radar_detail(attended=10, total=20, managed=None, **overrides):
    """Synthetic data matching GET /api/rollcall/{id}, without timetable fields."""
    students = [{"student_id": 900, "status": "absent"}] if managed is None else managed
    item = {
        "id": 123, "course_id": 42, "source": "radar", "type": "radar_rollcall",
        "created_at": "2026-10-13T02:00:00Z", "rollcall_time": "2026-10-13T02:00:00Z",
        "end_time": None, "is_number": False, "is_radar": True, "status": "in_progress",
        "student_rollcalls": students + [
            {"student_id": i + 1, "status": "on_call_fine" if i < attended else "absent", "score": 0}
            for i in range(total)
        ],
    }
    item.update(overrides)
    return item


def session_for(records=None, profile_id=900, radar_record=None):
    session = MagicMock()

    def get(url, **kwargs):
        if url.endswith("/api/profile"):
            return response({"id": profile_id})
        if url.endswith("/api/timetable_rollcalls"):
            return response({"rollcalls": [timetable()] if records is None else records})
        if url.endswith("/api/rollcall/123"):
            return response(radar_detail() if radar_record is None else radar_record)
        raise AssertionError(f"Unexpected GET: {url}")

    session.get.side_effect = get
    session.put.return_value = response({})
    return session
