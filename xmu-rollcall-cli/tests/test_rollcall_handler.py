import unittest
from unittest.mock import MagicMock, patch

from attendance_fixtures import ACCOUNT, detected, radar_detail, session_for
from xmu_rollcall.rollcall_handler import handle_rollcalls
from xmu_rollcall.attendance_guard import AttendanceDecision, AttendanceGuard


def rollcall(**overrides):
    data = {
        "course_title": "Course",
        "created_by_name": "Teacher",
        "department_name": "Department",
        "is_expired": False,
        "is_number": False,
        "is_radar": False,
        "rollcall_id": 123,
        "rollcall_status": "active",
        "scored": False,
        "status": "absent",
    }
    data.update(overrides)
    return data


class RollcallHandlerTest(unittest.TestCase):
    def allowed_guard(self):
        guard = MagicMock()
        guard.check.return_value = AttendanceDecision(allowed=True, number_code="0034")
        return guard

    def test_number_success_sends_feedback(self):
        notifier = MagicMock()
        notifier.send_rollcall_success.return_value.ok = True

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.send_code", return_value=True),
        ):
            result = handle_rollcalls({"rollcalls": [rollcall(is_number=True)]}, MagicMock(),
                                     attendance_guard=self.allowed_guard())

        self.assertEqual(result, [True])
        notifier.send_rollcall_success.assert_called_once()

    def test_radar_success_sends_feedback(self):
        notifier = MagicMock()
        notifier.send_rollcall_success.return_value.ok = True

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.send_radar", return_value=True),
        ):
            result = handle_rollcalls({"rollcalls": [rollcall(is_radar=True)]}, MagicMock(),
                                     attendance_guard=self.allowed_guard())

        self.assertEqual(result, [True])
        notifier.send_rollcall_success.assert_called_once()

    def test_qr_success_sends_feedback(self):
        notifier = MagicMock()
        notifier.send_rollcall_success.return_value.ok = True

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.handle_qr_rollcall", return_value=True),
        ):
            result = handle_rollcalls({"rollcalls": [rollcall()]}, MagicMock())

        self.assertEqual(result, [True])
        notifier.send_rollcall_success.assert_called_once()

    def test_non_absent_radar_does_not_submit(self):
        notifier = MagicMock()

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.send_radar") as send_radar,
        ):
            result = handle_rollcalls(
                {"rollcalls": [rollcall(is_radar=True, status="pending")]},
                MagicMock(),
            )

        self.assertEqual(result, [True])
        send_radar.assert_not_called()
        notifier.send_rollcall_success.assert_not_called()

    def test_waiting_guard_never_submits_or_notifies(self):
        for kind in ({"is_number": True}, {"is_radar": True}):
            with self.subTest(kind=kind):
                for decision in (AttendanceDecision(message="Waiting"),
                                 AttendanceDecision(complete=True, message="Finished")):
                    notifier = MagicMock()
                    guard = MagicMock()
                    guard.check.return_value = decision
                    with patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier), \
                            patch("xmu_rollcall.rollcall_handler.send_code") as code, \
                            patch("xmu_rollcall.rollcall_handler.send_radar") as radar:
                        result = handle_rollcalls({"rollcalls": [rollcall(**kind)]}, MagicMock(),
                                                  attendance_guard=guard)
                    self.assertEqual(result, [decision.complete])
                    code.assert_not_called()
                    radar.assert_not_called()
                    notifier.send_rollcall_success.assert_not_called()

    def test_absent_guard_cannot_bypass_threshold(self):
        for kind in ({"is_number": True}, {"is_radar": True}):
            with patch("xmu_rollcall.rollcall_handler.build_notifier"), \
                    patch("xmu_rollcall.rollcall_handler.send_code") as code, \
                    patch("xmu_rollcall.rollcall_handler.send_radar") as radar:
                self.assertEqual(handle_rollcalls({"rollcalls": [rollcall(**kind)]}, MagicMock()), [False])
            code.assert_not_called()
            radar.assert_not_called()

    def test_discovery_metadata_and_selected_code_are_forwarded(self):
        guard = self.allowed_guard()
        account = {"id": 1}
        session = MagicMock()
        data = rollcall(is_number=True, course_id=42, created_at="2026-10-13T02:00:00Z")
        with patch("xmu_rollcall.rollcall_handler.build_notifier"), \
                patch("xmu_rollcall.rollcall_handler.send_code", return_value=True) as code:
            handle_rollcalls({"rollcalls": [data]}, session, account=account, attendance_guard=guard)
        forwarded = guard.check.call_args.args[1]
        self.assertEqual(forwarded["course_id"], 42)
        self.assertEqual(forwarded["created_at"], "2026-10-13T02:00:00Z")
        self.assertEqual(guard.check.call_args.kwargs["account"], account)
        code.assert_called_once_with(session, 123, "0034")

    def test_radar_campaign_receives_verified_identity_course_and_end_time(self):
        end_time = "2099-01-01T00:00:00Z"
        session = session_for([], radar_record=radar_detail(end_time=end_time))
        with patch("xmu_rollcall.rollcall_handler.build_notifier"), \
                patch("xmu_rollcall.rollcall_handler.send_radar", return_value=True) as radar:
            result = handle_rollcalls({"rollcalls": [detected(is_radar=True, is_number=False)]},
                                      session, ACCOUNT, AttendanceGuard([1]))
        self.assertEqual(result, [True])
        radar.assert_called_once()
        self.assertEqual(radar.call_args.args, (session, 123))
        self.assertEqual(radar.call_args.kwargs["account_id"], 1)
        self.assertEqual(str(radar.call_args.kwargs["student_id"]), "900")
        self.assertEqual(radar.call_args.kwargs["course_id"], 42)
        self.assertEqual(radar.call_args.kwargs["end_time"], end_time)

    def test_failed_or_stopped_radar_campaign_does_not_notify_success(self):
        session = session_for([], radar_record=radar_detail())
        with patch("xmu_rollcall.rollcall_handler.build_notifier") as notifier, \
                patch("xmu_rollcall.rollcall_handler.send_radar", return_value=False) as radar:
            result = handle_rollcalls({"rollcalls": [detected(is_radar=True, is_number=False)]},
                                      session, ACCOUNT, AttendanceGuard([1]))
        self.assertEqual(result, [False])
        radar.assert_called_once()
        notifier.return_value.send_rollcall_success.assert_not_called()


if __name__ == "__main__":
    unittest.main()
