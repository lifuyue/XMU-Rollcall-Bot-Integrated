import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from attendance_fixtures import ACCOUNT, detected, response, session_for, timetable
from xmu_rollcall.attendance_guard import AttendanceGuard, REQUEST_TIMEOUT


NOW = datetime(2026, 10, 13, 2, 10, tzinfo=timezone.utc)


class AttendanceGuardTest(unittest.TestCase):
    def check(self, record=None, rollcall=None):
        session = session_for([timetable() if record is None else record])
        result = AttendanceGuard([1]).check(
            session, detected() if rollcall is None else rollcall, ACCOUNT, now=NOW,
        )
        session.put.assert_not_called()
        return result

    def test_requires_both_ratio_and_count_including_exact_boundary(self):
        for attended, total, allowed in [(10, 20, True), (9, 18, False), (10, 21, False),
                                          (11, 21, True), (9, 9, False), (0, 0, False)]:
            with self.subTest(attended=attended, total=total):
                self.assertEqual(self.check(timetable(attended, total)).allowed, allowed)

    def test_excludes_every_managed_student_from_both_counts(self):
        for signed, total, other_status, expected in [
            (10, 20, "absent", True), (9, 18, "on_call", False), (10, 21, "on_call", False),
        ]:
            with self.subTest(signed=signed, total=total, other_status=other_status):
                managed = [{"student_id": 900, "status": "absent"},
                           {"student_id": 901, "status": other_status}]
                session = session_for([timetable(signed, total, managed)])
                other = MagicMock()
                other.__enter__.return_value = other
                other.get.return_value = response({"id": 901})
                with patch("xmu_rollcall.attendance_guard.requests.Session", return_value=other), \
                        patch("xmu_rollcall.attendance_guard.load_session", return_value=True):
                    result = AttendanceGuard([1, 2]).check(session, detected(), ACCOUNT, now=NOW)
                self.assertEqual(result.allowed, expected)
                self.assertIn(f"{signed}/{total}", result.message)

    def test_unselected_complete_accounts_are_also_managed(self):
        config = {"accounts": [ACCOUNT, {"id": 2, "username": "two", "password": "p"},
                               {"id": 3, "username": "incomplete", "password": ""}]}
        with patch("xmu_rollcall.attendance_guard.load_config", return_value=config):
            guard = AttendanceGuard.for_accounts([ACCOUNT])
        self.assertEqual(guard.managed_account_ids, {"1", "2"})

    def test_unresolved_managed_identity_blocks_and_is_retried(self):
        guard = AttendanceGuard([1, 2])
        session = session_for()
        other = MagicMock()
        other.__enter__.return_value = other
        other.get.return_value = response({"id": 901})
        with patch("xmu_rollcall.attendance_guard.requests.Session", return_value=other), \
                patch("xmu_rollcall.attendance_guard.load_session", side_effect=[False, True]):
            self.assertFalse(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)
            self.assertTrue(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)

    def test_unknown_current_identity_blocks(self):
        for profile_id, account in [(None, ACCOUNT), (True, ACCOUNT), (900, None), (900, {"id": 8})]:
            with self.subTest(profile_id=profile_id, account=account):
                result = AttendanceGuard([1]).check(session_for(profile_id=profile_id), detected(), account, now=NOW)
                self.assertFalse(result.allowed)

    def test_matching_id_selects_code_even_when_another_record_appears_first(self):
        session = session_for([timetable(id=456, number_code="9999"), timetable()])
        result = AttendanceGuard([1]).check(session, detected(), ACCOUNT, now=NOW)
        self.assertTrue(result.allowed)
        self.assertEqual(result.number_code, "0034")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"], {"course_ids": "42", "rollcall_date": "2026-10-13"})
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], REQUEST_TIMEOUT)

    def test_mismatched_and_ambiguous_records_block(self):
        for records in [[timetable(id=456)], [timetable(), timetable()],
                        [timetable(course={"id": 43})], [timetable(rollcall_time="2026-10-12T02:00:00Z")]]:
            with self.subTest(records=records):
                result = AttendanceGuard([1]).check(session_for(records), detected(), ACCOUNT, now=NOW)
                self.assertFalse(result.allowed)

    def test_queries_utc_date_of_rollcall_time(self):
        # The timetable keys rollcalls by the UTC date of rollcall_time; a 17:00Z
        # start is filed under 10-12 even though campus-local time is already 10-13.
        moment = "2026-10-12T17:00:00Z"
        session = session_for([timetable(rollcall_time=moment)])
        result = AttendanceGuard([1]).check(session, detected(created_at=moment), ACCOUNT, now=NOW)
        self.assertTrue(result.allowed)
        self.assertEqual(session.get.call_args.kwargs["params"]["rollcall_date"], "2026-10-12")

    def test_missing_discovery_timestamp_checks_previous_day_by_exact_id(self):
        session = session_for()
        original_get = session.get.side_effect
        previous = timetable(rollcall_time="2026-10-12T15:55:00Z")

        def get(url, **kwargs):
            if url.endswith("/api/timetable_rollcalls"):
                day = kwargs["params"]["rollcall_date"]
                return response({"rollcalls": [] if day == "2026-10-13" else [previous]})
            return original_get(url, **kwargs)

        session.get.side_effect = get
        result = AttendanceGuard([1]).check(session, detected(created_at=None), ACCOUNT, now=NOW)
        self.assertTrue(result.allowed)
        self.assertEqual(session.get.call_args.kwargs["params"]["rollcall_date"], "2026-10-12")

    def test_no_fixed_delay_after_threshold_is_met(self):
        session = session_for([timetable(rollcall_time=NOW.isoformat())])
        with patch("time.sleep") as sleep:
            self.assertTrue(AttendanceGuard([1]).check(session, detected(), ACCOUNT, now=NOW).allowed)
        sleep.assert_not_called()

    def test_finished_or_expired_records_are_terminal_without_submission(self):
        for record in [timetable(status="finished"), timetable(end_time=NOW.isoformat())]:
            with self.subTest(record=record):
                result = self.check(record)
                self.assertFalse(result.allowed)
                self.assertTrue(result.complete)

    def test_expiry_is_checked_after_network_reads(self):
        session = session_for([timetable(end_time="2026-10-13T02:10:01Z")])
        later = datetime(2026, 10, 13, 2, 10, 2, tzinfo=timezone.utc)
        with patch("xmu_rollcall.attendance_guard.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.side_effect = [NOW, later]
            result = AttendanceGuard([1]).check(session, detected(), ACCOUNT)
        self.assertTrue(result.complete)
        self.assertFalse(result.allowed)

    def test_invalid_metadata_blocks(self):
        for changes in [{"status": "unknown"}, {"status": None}, {"end_time": "bad"},
                        {"rollcall_time": "2026-10-13T02:00:00"}, {"is_number": False},
                        {"is_number": 1}, {"course": None}]:
            with self.subTest(changes=changes):
                self.assertFalse(self.check(timetable(**changes)).allowed)
        self.assertFalse(self.check(rollcall=detected(course_id=None)).allowed)

    def test_radar_has_same_threshold_and_does_not_require_number_code(self):
        for attended, expected in [(9, False), (10, True)]:
            result = self.check(timetable(attended, is_number=False, number_code=None),
                                detected(is_number=False, is_radar=True))
            self.assertEqual(result.allowed, expected)

    def test_missing_and_invalid_number_codes_block(self):
        for code in [None, "", 1234, "12", "12345", "abcd", "１２３４"]:
            with self.subTest(code=code):
                self.assertFalse(self.check(timetable(number_code=code)).allowed)

    def test_invalid_or_incomplete_rosters_block(self):
        good = timetable()["student_rollcalls"]
        for roster in [None, {}, [], good[1:], good + [good[1]],
                       good + [{"student_id": 33}], good + [{"status": "on_call"}],
                       good + [{"student_id": 33, "status": ""}], good + [None]]:
            with self.subTest(roster=roster):
                self.assertFalse(self.check(timetable(student_rollcalls=roster)).allowed)

    def test_only_confirmed_attendance_counts(self):
        record = timetable()
        record["student_rollcalls"][1]["status"] = "on_call_fine"
        self.assertTrue(self.check(record).allowed)
        for status in ["late", "leave", "pending", "unknown"]:
            record["student_rollcalls"][1]["status"] = status
            self.assertFalse(self.check(record).allowed)

    def test_already_answered_is_terminal_and_pending_is_not_submitted(self):
        for status in ["on_call", "on_call_fine", "pending"]:
            record = timetable()
            record["student_rollcalls"][0]["status"] = status
            result = self.check(record)
            self.assertFalse(result.allowed)
            self.assertEqual(result.complete, status != "pending")

    def test_bad_responses_and_timeouts_block_without_caching_allowance(self):
        for error in [requests.Timeout(), requests.HTTPError(), ValueError()]:
            with self.subTest(error=error):
                session = session_for()
                guard = AttendanceGuard([1])
                self.assertTrue(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)
                session.get.side_effect = error
                decision = guard.check(session, detected(), ACCOUNT, now=NOW)
                self.assertFalse(decision.allowed)
                self.assertFalse(decision.complete)
        for data in [{}, {"rollcalls": {}}, {"rollcalls": [None]}, []]:
            session = session_for()
            original_get = session.get.side_effect
            session.get.side_effect = lambda url, **kw: (response(data) if url.endswith("/api/timetable_rollcalls")
                                                        else original_get(url, **kw))
            self.assertFalse(AttendanceGuard([1]).check(session, detected(), ACCOUNT, now=NOW).allowed)

    def test_fresh_roster_is_read_on_every_check(self):
        record = timetable(attended=9)
        session = session_for([record])
        guard = AttendanceGuard([1])
        self.assertFalse(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)
        record["student_rollcalls"][10]["status"] = "on_call"
        self.assertTrue(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)
        record["student_rollcalls"][10]["status"] = "absent"
        self.assertFalse(guard.check(session, detected(), ACCOUNT, now=NOW).allowed)


if __name__ == "__main__":
    unittest.main()
