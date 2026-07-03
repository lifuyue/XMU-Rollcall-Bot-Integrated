import unittest
from unittest.mock import MagicMock, patch

from xmu_rollcall.rollcall_handler import handle_rollcalls


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
    def test_number_success_sends_feedback(self):
        notifier = MagicMock()
        notifier.send_rollcall_success.return_value.ok = True

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.send_code", return_value=True),
        ):
            result = handle_rollcalls({"rollcalls": [rollcall(is_number=True)]}, MagicMock())

        self.assertEqual(result, [True])
        notifier.send_rollcall_success.assert_called_once()

    def test_radar_success_sends_feedback(self):
        notifier = MagicMock()
        notifier.send_rollcall_success.return_value.ok = True

        with (
            patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier),
            patch("xmu_rollcall.rollcall_handler.send_radar", return_value=True),
        ):
            result = handle_rollcalls({"rollcalls": [rollcall(is_radar=True)]}, MagicMock())

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


if __name__ == "__main__":
    unittest.main()
