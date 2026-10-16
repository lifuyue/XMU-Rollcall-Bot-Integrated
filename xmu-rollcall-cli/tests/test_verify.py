import unittest
from unittest.mock import MagicMock, patch

import requests
from urllib3.util import Timeout

from attendance_fixtures import radar_detail, response
from xmu_rollcall.attendance_guard import REQUEST_TIMEOUT
from xmu_rollcall.verify import send_code, send_radar


class AnswerSubmissionTest(unittest.TestCase):
    def run_radar(self, session):
        session.get.return_value = response(radar_detail())
        with patch("xmu_rollcall.verify.time.monotonic", return_value=1000.0), \
                patch("xmu_rollcall.verify.time.sleep"):
            return send_radar(session, 123, student_id=900, course_id=42)

    def assert_radar_timeout(self, call):
        timeout = call.kwargs["timeout"]
        self.assertIsInstance(timeout, Timeout)
        self.assertEqual(timeout.total, 10.0)
        self.assertEqual(timeout.connect_timeout, 5)
        self.assertEqual(timeout.read_timeout, 10)

    def test_number_uses_supplied_code_without_old_api_or_sleep(self):
        session = MagicMock()
        session.put.return_value = response({})
        with patch("time.sleep") as sleep:
            self.assertTrue(send_code(session, 123, "0034"))
        session.get.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(session.put.call_args.kwargs["json"]["numberCode"], "0034")
        self.assertEqual(session.put.call_args.kwargs["timeout"], REQUEST_TIMEOUT)

    def test_invalid_code_never_submits(self):
        for code in (None, "", "abcd", "123", 1234):
            session = MagicMock()
            self.assertFalse(send_code(session, 123, code))
            session.put.assert_not_called()

    def test_number_http_rejection_and_timeout_return_failure(self):
        session = MagicMock()
        session.put.return_value = response({}, status=400)
        self.assertFalse(send_code(session, 123, "0034"))
        session.put.side_effect = requests.Timeout()
        self.assertFalse(send_code(session, 123, "0034"))

    def test_radar_success_is_bounded_and_does_not_need_json_body(self):
        session = MagicMock()
        session.put.return_value = response({})
        session.put.return_value.json.side_effect = ValueError()
        self.assertTrue(self.run_radar(session))
        session.put.return_value.json.assert_not_called()
        self.assertEqual(session.put.call_count, 1)
        self.assertEqual(session.get.call_count, 1)
        self.assert_radar_timeout(session.put.call_args)
        self.assert_radar_timeout(session.get.call_args)

    def test_radar_invalid_distance_and_response_return_failure(self):
        for data in ({}, [], {"distance": None}, {"distance": "100"}, {"distance": True},
                     {"distance": -1}, {"distance": float("nan")}, {"distance": float("inf")}):
            with self.subTest(data=data):
                session = MagicMock()
                session.put.return_value = response(data, status=400)
                self.assertFalse(self.run_radar(session))
                self.assertEqual(session.put.call_count, 31)
                self.assertEqual(session.get.call_count, 31)
                session.put.return_value.json.assert_called_once()
        for error in (requests.Timeout(), ValueError()):
            session = MagicMock()
            session.put.side_effect = error
            self.assertFalse(self.run_radar(session))
            self.assertEqual(session.put.call_count, 31)
            self.assertEqual(session.get.call_count, 31)

    def test_radar_all_attempts_have_timeouts(self):
        session = MagicMock()
        session.put.side_effect = [response({"distance": 25000}, 400),
                                   response({"distance": 25000}, 400),
                                   response({}, 400), response({})]
        self.assertTrue(self.run_radar(session))
        self.assertEqual(session.put.call_count, 4)
        self.assertEqual([call[0] for call in session.method_calls], ["get", "put"] * 4)
        for call in session.get.call_args_list + session.put.call_args_list:
            self.assert_radar_timeout(call)


if __name__ == "__main__":
    unittest.main()
