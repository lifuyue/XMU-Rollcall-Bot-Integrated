"""Offline Radar diagnostics tests; all school requests are mocked."""

import contextlib
import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests
from urllib3.util import Timeout

from attendance_fixtures import radar_detail, response
from xmu_rollcall.verify import headers, send_radar


SECRET = "private-cookie-and-response-secret"
ATTEMPTS = ["probe_1", "probe_2", "candidate_1", "candidate_2"]
FIXED_ATTEMPTS = ["fixed_1", "fixed_2", "fixed_3"] + [
    f"fixed_{index}_{direction}" for index in range(1, 4)
    for direction in ("east", "south", "west", "north")
]


class RadarDiagnosticsTest(unittest.TestCase):
    def run_radar(self, session, worker="xmu-account-2"):
        session.get.return_value = response(radar_detail())
        output = io.StringIO()
        thread = threading.current_thread()
        previous_name = thread.name
        try:
            thread.name = worker
            with contextlib.redirect_stdout(output), \
                    patch("xmu_rollcall.verify.time.monotonic", return_value=1000.0), \
                    patch("xmu_rollcall.verify.time.sleep"):
                result = send_radar(session, 123, student_id=900, course_id=42)
        finally:
            thread.name = previous_name
        raw = output.getvalue()
        self.assertNotIn(SECRET, raw)
        events = [json.loads(line[len("[Radar] "):]) for line in raw.splitlines()
                  if line.startswith("[Radar] ")]
        self.assertTrue(events, "Radar attempts must emit structured diagnostics")
        allowed = {"rollcall_id", "worker", "attempt", "phase", "http_status", "event",
                   "exception_type", "reason", "json_line", "json_column", "json_type", "distance_type", "round"}
        for event in events:
            self.assertEqual(event["rollcall_id"], 123)
            self.assertEqual(event["worker"], worker)
            self.assertIn(event["attempt"], ATTEMPTS + ["solve"] + FIXED_ATTEMPTS)
            self.assertIn(event["phase"], {"request", "json", "distance", "response", "geometry", "status",
                                          "status_request", "status_response", "status_json", "status_validation",
                                          "budget"})
            self.assertIn(event["event"], {"http_response", "exception", "failed", "fallback_start", "success",
                                          "status_response", "stopped", "round_start", "round_end"})
            self.assertIn("http_status", event)
            self.assertTrue(event["http_status"] is None or type(event["http_status"]) is int)
            if "round" in event:
                self.assertIn(event["round"], (None, 0, 1, 2))
            self.assertFalse(set(event) - allowed, "Diagnostics must not contain bodies, payloads or coordinates")
        self.assertEqual(session.get.call_count, session.put.call_count)
        for call in session.get.call_args_list:
            self.assertEqual(call.args, ("https://lnt.xmu.edu.cn/api/rollcall/123",))
        return result, events

    def assert_event(self, events, **expected):
        self.assertTrue(any(all(row.get(key) == value for key, value in expected.items())
                            for row in events), f"Missing event {expected}; got {events}")

    def test_network_failures_identify_worker_and_request_without_leaking_exception(self):
        for worker, error in [("xmu-account-1", requests.Timeout(SECRET)),
                              ("xmu-account-2", requests.ConnectionError(SECRET)),
                              ("xmu-account-3", requests.ReadTimeout(SECRET))]:
            with self.subTest(worker=worker):
                session = MagicMock()
                session.put.side_effect = error
                result, events = self.run_radar(session, worker)
                self.assertFalse(result)
                self.assertEqual(session.put.call_count, 31)
                self.assert_event(events, event="exception", attempt="probe_1", phase="request",
                                  http_status=None, exception_type=type(error).__name__, reason="request_failed")

    def test_second_probe_non_json_400_is_attributed_without_response_content(self):
        first = response({"distance": 25000}, 400)
        second = response({}, 400)
        second.text = SECRET
        error = json.JSONDecodeError(SECRET, "\n" + SECRET, 2)
        second.json.side_effect = error
        session = MagicMock()
        session.put.side_effect = [first, second] + [response({}, 403), response({}, 500), response({}, 403)] * 10
        result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 32)
        self.assert_event(events, event="http_response", attempt="probe_2", phase="response", http_status=400)
        self.assert_event(events, event="exception", attempt="probe_2", phase="json", http_status=400,
                          exception_type="JSONDecodeError", reason="json_decode_failed",
                          json_line=error.lineno, json_column=error.colno)

    def test_distance_validation_reports_types_without_values(self):
        for data, json_type, distance_type, reason in [
            ({"private": SECRET}, "dict", "missing", "missing radar distance"),
            ([SECRET], "list", "unavailable", "missing radar distance"),
            ({"distance": SECRET}, "dict", "str", "missing radar distance"),
            ({"distance": None}, "dict", "NoneType", "missing radar distance"),
            ({"distance": True}, "dict", "bool", "missing radar distance"),
            ({"distance": float("nan")}, "dict", "float", "invalid radar distance"),
            ({"distance": float("inf")}, "dict", "float", "invalid radar distance"),
            ({"distance": -1}, "dict", "int", "invalid radar distance"),
        ]:
            with self.subTest(json_type=json_type, distance_type=distance_type, reason=reason):
                session = MagicMock()
                session.put.return_value = response(data, 400)
                result, events = self.run_radar(session)
                self.assertFalse(result)
                self.assertEqual(session.put.call_count, 31)
                session.put.return_value.json.assert_called_once()
                self.assert_event(events, event="exception", attempt="probe_1", phase="distance",
                                  http_status=400, exception_type="ValueError", reason=reason,
                                  json_type=json_type, distance_type=distance_type)

    def test_unrecognized_distance_exception_uses_fixed_reason(self):
        session = MagicMock()
        session.put.return_value = response({"distance": 25000}, 400)
        with patch("xmu_rollcall.verify._distance", side_effect=ValueError(SECRET)):
            result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 31)
        self.assert_event(events, event="exception", attempt="probe_1", phase="distance",
                          http_status=400, exception_type="ValueError", reason="distance_validation_failed")

    def test_no_circle_intersection_stops_before_candidate_requests(self):
        session = MagicMock()
        session.put.side_effect = [response({"distance": 1}, 400), response({"distance": 1}, 400)] + [
            response({}, 403), response({}, 500), response({}, 403)] * 10
        result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 32)
        self.assertFalse(any(row.get("attempt", "").startswith("candidate_") for row in events))
        self.assert_event(events, event="failed", attempt="solve", phase="geometry",
                          http_status=None, reason="no_circle_intersection")

    def test_geometry_exception_is_not_misreported_as_request_failure(self):
        session = MagicMock()
        session.put.side_effect = [response({"distance": 25000}, 400), response({"distance": 25000}, 400)] + [
            response({}, 403), response({}, 500), response({}, 403)] * 10
        with patch("xmu_rollcall.verify.math.sqrt", side_effect=OverflowError(SECRET)):
            result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 32)
        self.assert_event(events, event="exception", attempt="solve", phase="geometry",
                          http_status=None, exception_type="OverflowError", reason="geometry_failed")

    def test_rejected_candidates_preserve_request_order_payloads_and_timeouts(self):
        replies = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                   response({"private": SECRET}, 403), response({"private": SECRET}, 500)] + [
            response({"private": SECRET}, status) for status in [403, 500, 403] * 10]
        session = MagicMock()
        session.put.side_effect = replies
        with patch("xmu_rollcall.verify.uuid.uuid4", side_effect=[f"device-{i}" for i in range(1, 35)]):
            result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 34)
        actual_responses = [(row["attempt"], row["http_status"]) for row in events if row["event"] == "http_response"]
        self.assertEqual(actual_responses, list(zip(ATTEMPTS + FIXED_ATTEMPTS * 2,
                                                   [400, 400, 403, 500] + [403, 500, 403] * 10)))
        fixed_events = [row for row in events if row["event"] == "http_response"
                        and row["attempt"].startswith("fixed_")]
        self.assertEqual([row["round"] for row in fixed_events], [1] * 15 + [2] * 15)
        self.assert_event(events, event="failed", attempt="candidate_2", phase="response",
                          http_status=500, reason="all_candidates_rejected")
        coordinates = [(24.3, 118.0), (24.6, 118.2),
                       (24.522933361048867, 117.96798398027131),
                       (24.377066638951135, 118.23201601972869),
                       (24.6082, 118.2953), (24.6087, 118.3090), (24.6075, 118.2950)]
        for index, call in enumerate(session.put.call_args_list, 1):
            self.assertEqual(call.args, ("https://lnt.xmu.edu.cn/api/rollcall/123/answer",))
            timeout = call.kwargs["timeout"]
            self.assertIsInstance(timeout, Timeout)
            self.assertEqual(timeout.total, 10.0 if index <= 4 else 60.0)
            self.assertEqual(timeout.connect_timeout, 5)
            self.assertEqual(timeout.read_timeout, 10 if index <= 4 else 12)
            self.assertEqual(call.kwargs["headers"], headers)
            payload = dict(call.kwargs["json"])
            latitude, longitude = payload.pop("latitude"), payload.pop("longitude")
            if index <= len(coordinates):
                self.assertAlmostEqual(latitude, coordinates[index - 1][0], places=10)
                self.assertAlmostEqual(longitude, coordinates[index - 1][1], places=10)
            self.assertEqual(payload, {"accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
                                       "deviceId": f"device-{index}", "heading": None, "speed": None})
        for reply in replies[2:]:
            reply.json.assert_not_called()

    def test_200_at_every_attempt_returns_immediately_without_parsing_its_json(self):
        for successful_attempt in range(4):
            with self.subTest(attempt=ATTEMPTS[successful_attempt]):
                replies = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                           response({}, 403), response({}, 500)]
                replies[successful_attempt].status_code = 200
                replies[successful_attempt].json.side_effect = ValueError(SECRET)
                session = MagicMock()
                session.put.side_effect = replies
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assertEqual(session.put.call_count, successful_attempt + 1)
                replies[successful_attempt].json.assert_not_called()
                self.assertFalse(any(row["event"] in {"failed", "exception"} for row in events))
                self.assertFalse(any(row["event"] == "fallback_start" for row in events))
                self.assert_event(events, event="http_response", attempt=ATTEMPTS[successful_attempt],
                                  phase="response", http_status=200)


if __name__ == "__main__":
    unittest.main()
