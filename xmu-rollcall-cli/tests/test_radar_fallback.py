"""Fixed-location retries use mocked requests only, after the original Radar path."""

import contextlib
import io
import json
import math
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from attendance_fixtures import ACCOUNT, detected, radar_detail, response, session_for
from urllib3.util import Timeout

from xmu_rollcall.attendance_guard import AttendanceGuard
from xmu_rollcall.rollcall_handler import handle_rollcalls
from xmu_rollcall.verify import headers, send_radar, _RadarFallback


FIXED_LOCATIONS = [(24.6082, 118.2953), (24.6087, 118.3090), (24.6075, 118.2950)]
FIXED_ATTEMPTS = ["fixed_1", "fixed_2", "fixed_3"] + [
    f"fixed_{index}_{direction}" for index in range(1, 4)
    for direction in ("east", "south", "west", "north")
]
SECRET = "private-fallback-cookie-and-response-secret"


def fixed_response(status):
    reply = response({"private": SECRET}, status)
    reply.text = SECRET
    reply.json.side_effect = AssertionError("Fixed-location responses must not be decoded")
    return reply


class RadarFallbackTest(unittest.TestCase):
    def run_radar(self, session, *, state=None, clock=None, **kwargs):
        if session.get.side_effect is None:
            session.get.return_value = response(radar_detail() if state is None else state)
        clock = [1000.0] if clock is None else clock
        output = io.StringIO()
        thread = threading.current_thread()
        previous_name = thread.name
        try:
            thread.name = "xmu-account-3"
            with contextlib.redirect_stdout(output), \
                    patch("xmu_rollcall.verify.time.monotonic", side_effect=lambda: clock[0]), \
                    patch("xmu_rollcall.verify.time.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
                result = send_radar(session, 123, student_id=900, course_id=42, **kwargs)
        finally:
            thread.name = previous_name
        raw = output.getvalue()
        self.assertNotIn(SECRET, raw)
        for coordinate in ("24.6082", "118.2953", "24.6087", "118.309", "24.6075", "118.295"):
            self.assertNotIn(coordinate, raw)
        events = [json.loads(line[len("[Radar] "):]) for line in raw.splitlines()
                  if line.startswith("[Radar] ")]
        for event in events:
            self.assertEqual(event["rollcall_id"], 123)
            self.assertEqual(event["worker"], "xmu-account-3")
            self.assertFalse({"latitude", "longitude", "payload", "response_body", "body", "data"} & set(event))
        return result, events

    def assert_event(self, events, **expected):
        self.assertTrue(any(all(event.get(key) == value for key, value in expected.items())
                            for event in events), f"Missing event {expected}; got {events}")

    def assert_fixed_calls(self, session, original_count, count=3):
        self.assertEqual(session.put.call_count, original_count + count)
        for index, call in enumerate(session.put.call_args_list[original_count:]):
            self.assertEqual(call.args, ("https://lnt.xmu.edu.cn/api/rollcall/123/answer",))
            self.assertEqual(call.kwargs["headers"], headers)
            self.assertIs(call.kwargs["allow_redirects"], False)
            timeout = call.kwargs["timeout"]
            self.assertIsInstance(timeout, Timeout)
            self.assertGreater(timeout.total, 0)
            self.assertLessEqual(timeout.total, 60)
            self.assertLessEqual(timeout.connect_timeout, 5)
            self.assertLessEqual(timeout.read_timeout, 12)
            payload = call.kwargs["json"]
            if index % 15 < 3:
                self.assertEqual((payload["latitude"], payload["longitude"]), FIXED_LOCATIONS[index % 15])
            self.assertEqual(set(payload), {"accuracy", "altitude", "altitudeAccuracy", "deviceId",
                                            "heading", "latitude", "longitude", "speed"})
            self.assertEqual({key: value for key, value in payload.items()
                              if key not in {"deviceId", "latitude", "longitude"}},
                             {"accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
                              "heading": None, "speed": None})

    def test_original_failure_paths_all_reach_the_three_fixed_locations(self):
        non_json = response({}, 400)
        non_json.json.side_effect = json.JSONDecodeError(SECRET, SECRET, 0)
        scenarios = [
            ("missing_distance", [response({}, 400)]),
            ("invalid_distance", [response({"distance": -1}, 400)]),
            ("nan_distance", [response({"distance": float("nan")}, 400)]),
            ("non_json", [non_json]),
            ("second_probe_timeout", [response({"distance": 25000}, 400), requests.Timeout(SECRET)]),
            ("no_intersection", [response({"distance": 1}, 400), response({"distance": 1}, 400)]),
            ("candidate_rejections", [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                                      response({}, 403), response({}, 500)]),
        ]
        for name, original in scenarios:
            with self.subTest(scenario=name):
                fixed = [fixed_response(403), fixed_response(500), fixed_response(200)]
                session = MagicMock()
                session.put.side_effect = original + fixed
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, len(original))
                self.assertEqual(sum(event["event"] == "fallback_start" for event in events), 1)
                self.assert_event(events, event="http_response", attempt="fixed_1", http_status=403)
                self.assert_event(events, event="http_response", attempt="fixed_2", http_status=500)
                self.assert_event(events, event="success", attempt="fixed_3", http_status=200)
                self.assertFalse(any(event.get("reason") == "all_fixed_locations_rejected" for event in events))
                for reply in fixed:
                    reply.json.assert_not_called()

    def test_first_fixed_success_stops_before_any_later_fixed_request(self):
        fixed = fixed_response(200)
        session = MagicMock()
        session.put.side_effect = [response({}, 400), fixed]
        result, events = self.run_radar(session)
        self.assertTrue(result)
        self.assert_fixed_calls(session, 1, count=1)
        self.assert_event(events, event="success", attempt="fixed_1", http_status=200)
        self.assertFalse(any(event.get("attempt") in {"fixed_2", "fixed_3"} for event in events))
        fixed.json.assert_not_called()

    def test_second_fixed_success_stops_before_the_third_fixed_request(self):
        fixed = [fixed_response(403), fixed_response(200)]
        session = MagicMock()
        session.put.side_effect = [response({}, 400)] + fixed
        result, events = self.run_radar(session)
        self.assertTrue(result)
        self.assert_fixed_calls(session, 1, count=2)
        self.assert_event(events, event="success", attempt="fixed_2", http_status=200)
        self.assertFalse(any(event.get("attempt") == "fixed_3" for event in events))
        for reply in fixed:
            reply.json.assert_not_called()

    def test_first_fixed_exception_is_logged_and_second_fixed_is_still_attempted(self):
        for error in [requests.Timeout(SECRET), requests.ConnectionError(SECRET), ValueError(SECRET),
                      TypeError(SECRET), OverflowError(SECRET), ZeroDivisionError(SECRET)]:
            with self.subTest(exception_type=type(error).__name__):
                fixed = fixed_response(200)
                session = MagicMock()
                session.put.side_effect = [response({}, 400), error, fixed]
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, 1, count=2)
                self.assert_event(events, event="exception", attempt="fixed_1", phase="request",
                                  http_status=None, exception_type=type(error).__name__, reason="request_failed")
                self.assert_event(events, event="success", attempt="fixed_2", http_status=200)
                self.assertFalse(any(event.get("attempt") == "fixed_3" for event in events))
                fixed.json.assert_not_called()

    def test_second_fixed_exception_is_logged_and_third_fixed_is_still_attempted(self):
        for error in [requests.Timeout(SECRET), requests.ConnectionError(SECRET), ValueError(SECRET),
                      TypeError(SECRET), OverflowError(SECRET), ZeroDivisionError(SECRET)]:
            with self.subTest(exception_type=type(error).__name__):
                first, third = fixed_response(403), fixed_response(200)
                session = MagicMock()
                session.put.side_effect = [response({}, 400), first, error, third]
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, 1)
                self.assert_event(events, event="exception", attempt="fixed_2", phase="request",
                                  http_status=None, exception_type=type(error).__name__, reason="request_failed")
                self.assert_event(events, event="success", attempt="fixed_3", http_status=200)
                first.json.assert_not_called()
                third.json.assert_not_called()

    def test_all_fixed_rejections_or_exceptions_are_bounded_and_report_failure(self):
        for fixed in [
            [fixed_response(403), fixed_response(500), fixed_response(403)],
            [requests.Timeout(SECRET), requests.ConnectionError(SECRET), requests.Timeout(SECRET)],
            [fixed_response(500), requests.Timeout(SECRET), fixed_response(403)],
            [requests.Timeout(SECRET), fixed_response(403), requests.Timeout(SECRET)],
        ]:
            with self.subTest(fixed=fixed):
                session = MagicMock()
                session.put.side_effect = [response({}, 400)] + fixed * 10
                result, events = self.run_radar(session)
                self.assertFalse(result)
                self.assert_fixed_calls(session, 1, count=30)
                self.assertEqual(sum(event["event"] == "fallback_start" for event in events), 1)
                self.assert_event(events, event="failed", attempt="fixed_3_north", round=2, reason="all_fixed_locations_rejected")
                for reply in fixed:
                    if not isinstance(reply, Exception):
                        reply.json.assert_not_called()

    def test_thirty_four_requests_maximum_with_fresh_device_ids_after_candidate_failures(self):
        fixed = [fixed_response(403), fixed_response(500), fixed_response(403)]
        session = MagicMock()
        session.put.side_effect = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                                   response({}, 403), response({}, 500)] + fixed * 10
        ids = [f"device-{index}" for index in range(1, 35)]
        with patch("xmu_rollcall.verify.uuid.uuid4", side_effect=ids) as new_uuid:
            result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assert_fixed_calls(session, 4, count=30)
        self.assertEqual(new_uuid.call_count, 34)
        self.assertEqual([call.kwargs["json"]["deviceId"] for call in session.put.call_args_list], ids)
        self.assertEqual([event["attempt"] for event in events if event["event"] == "http_response"],
                         ["probe_1", "probe_2", "candidate_1", "candidate_2"] + FIXED_ATTEMPTS * 2)
        self.assert_event(events, event="failed", attempt="fixed_3_north", round=2, http_status=403,
                          reason="all_fixed_locations_rejected")
        for reply in fixed:
            reply.json.assert_not_called()

    def test_success_at_each_original_attempt_never_uses_fixed_locations(self):
        for successful_attempt in range(4):
            with self.subTest(successful_attempt=successful_attempt):
                replies = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                           response({}, 403), response({}, 500)]
                replies[successful_attempt].status_code = 200
                replies[successful_attempt].json.side_effect = AssertionError("200 must not be decoded")
                session = MagicMock()
                session.put.side_effect = replies
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assertEqual(session.put.call_count, successful_attempt + 1)
                self.assertFalse(any(event["event"] == "fallback_start" or
                                     str(event.get("attempt", "")).startswith("fixed_") for event in events))
                replies[successful_attempt].json.assert_not_called()

    def test_two_rounds_keep_centers_first_then_100m_cardinal_points(self):
        session = MagicMock()
        session.put.side_effect = [response({}, 400)] + [fixed_response(403) for _ in range(30)]
        result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assert_fixed_calls(session, 1, count=30)
        fixed_events = [event for event in events if event["event"] == "http_response"
                        and event["attempt"].startswith("fixed_")]
        self.assertEqual([(event["round"], event["attempt"]) for event in fixed_events],
                         [(round_number, attempt) for round_number in (1, 2) for attempt in FIXED_ATTEMPTS])
        coordinates = [(call.kwargs["json"]["latitude"], call.kwargs["json"]["longitude"])
                       for call in session.put.call_args_list[1:]]
        self.assertEqual(coordinates[:15], coordinates[15:])
        self.assertEqual(coordinates[:3], FIXED_LOCATIONS)
        self.assertEqual(len(set(coordinates[:15])), 15)
        for index, (latitude, longitude) in enumerate(FIXED_LOCATIONS):
            for direction, (actual_lat, actual_lon) in zip(
                    ("east", "south", "west", "north"), coordinates[3 + index * 4:7 + index * 4]):
                with self.subTest(center=index + 1, direction=direction):
                    # Measure the resulting coordinates independently of the offset implementation.
                    delta_lat = math.radians(actual_lat - latitude)
                    delta_lon = math.radians(actual_lon - longitude)
                    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(math.radians(latitude)) * \
                        math.cos(math.radians(actual_lat)) * math.sin(delta_lon / 2) ** 2
                    distance = 2 * 6371000 * math.asin(math.sqrt(haversine))
                    self.assertAlmostEqual(distance, 100, delta=0.05)
                    if direction in {"east", "west"}:
                        self.assertAlmostEqual(actual_lat, latitude, places=7)
                        self.assertEqual(actual_lon > longitude, direction == "east")
                        self.assertNotEqual(actual_lon, round(actual_lon, 4))
                    else:
                        self.assertAlmostEqual(actual_lon, longitude, places=7)
                        self.assertEqual(actual_lat > latitude, direction == "north")
                        self.assertNotEqual(actual_lat, round(actual_lat, 4))

    def test_success_at_a_cardinal_point_or_in_round_two_stops_immediately(self):
        for fixed_index in (3, 14, 15, 29):
            with self.subTest(fixed_index=fixed_index):
                session = MagicMock()
                session.put.side_effect = [response({}, 400)] + [fixed_response(403) for _ in range(fixed_index)] + \
                    [fixed_response(200)]
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assertEqual(session.put.call_count, fixed_index + 2)
                self.assert_event(events, event="success", attempt=FIXED_ATTEMPTS[fixed_index % 15],
                                  round=fixed_index // 15 + 1, http_status=200)

    def test_every_submission_rechecks_current_status_with_a_bounded_get(self):
        session = MagicMock()
        session.put.side_effect = [response({}, 400), fixed_response(403), fixed_response(200)]
        result, _ = self.run_radar(session)
        self.assertTrue(result)
        requests_made = [call for call in session.mock_calls if call[0] in {"get", "put"}]
        self.assertEqual([call[0] for call in requests_made], ["get", "put"] * 3)
        for call in session.get.call_args_list:
            self.assertEqual(call.args, ("https://lnt.xmu.edu.cn/api/rollcall/123",))
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertIsInstance(call.kwargs["timeout"], Timeout)
            self.assertGreater(call.kwargs["timeout"].total, 0)
            self.assertLessEqual(call.kwargs["timeout"].total, 60)

    def test_already_answered_or_finished_before_first_request_never_submits(self):
        for status, expected in (("on_call", True), ("on_call_fine", True)):
            with self.subTest(own_status=status):
                session = MagicMock()
                result, _ = self.run_radar(session, state=radar_detail(managed=[{"student_id": 900, "status": status}]))
                self.assertEqual(result, expected)
                session.put.assert_not_called()
        for state in (radar_detail(status="finished"), radar_detail(end_time="2000-01-01T00:00:00Z")):
            with self.subTest(state=state["status"], end_time=state["end_time"]):
                session = MagicMock()
                result, _ = self.run_radar(session, state=state)
                self.assertFalse(result)
                session.put.assert_not_called()

    def test_invalid_status_identity_or_roster_stops_without_fallback(self):
        states = [[], radar_detail(id=456), radar_detail(course_id=43), radar_detail(is_radar=False),
                  radar_detail(status="unknown"), radar_detail(end_time="invalid"),
                  radar_detail(managed=[]), radar_detail(student_rollcalls=None),
                  radar_detail(managed=[{"student_id": 900, "status": "unknown"}]),
                  radar_detail(managed=[{"student_id": 900, "status": "absent"},
                                        {"student_id": "900", "status": "on_call_fine"}])]
        for state in states:
            with self.subTest(state=state):
                session = MagicMock()
                result, events = self.run_radar(session, state=state)
                self.assertFalse(result)
                session.put.assert_not_called()
                self.assertFalse(any(event["event"] == "fallback_start" for event in events))

    def test_status_get_error_does_not_turn_into_a_location_retry(self):
        errors = [requests.Timeout(SECRET), requests.ConnectionError(SECRET), json.JSONDecodeError(SECRET, SECRET, 0)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                session = MagicMock()
                session.get.side_effect = error
                result, events = self.run_radar(session)
                self.assertFalse(result)
                session.get.assert_called_once()
                session.put.assert_not_called()
                self.assertFalse(any(event["event"] == "fallback_start" for event in events))
        session = MagicMock()
        denied = response({}, 403)
        denied.raise_for_status.side_effect = requests.HTTPError(SECRET)
        session.get.side_effect = [denied]
        result, _ = self.run_radar(session)
        self.assertFalse(result)
        session.put.assert_not_called()

        session = MagicMock()
        malformed = response({})
        malformed.json.side_effect = json.JSONDecodeError(SECRET, SECRET, 0)
        session.get.side_effect = [malformed]
        result, events = self.run_radar(session)
        self.assertFalse(result)
        session.put.assert_not_called()
        self.assert_event(events, event="exception", phase="status_json", exception_type="JSONDecodeError")

    def test_unauthorized_or_rate_limited_answer_stops_the_entire_campaign(self):
        for status in (401, 429):
            for preceding in ([], [response({}, 400)]):
                with self.subTest(status=status, preceding=len(preceding)):
                    session = MagicMock()
                    denied = fixed_response(status)
                    session.put.side_effect = preceding + [denied]
                    result, _ = self.run_radar(session)
                    self.assertFalse(result)
                    self.assertEqual(session.put.call_count, len(preceding) + 1)
                    denied.json.assert_not_called()

    def test_mid_campaign_finish_or_manual_answer_stops_before_next_put(self):
        for state, expected in [(radar_detail(status="finished"), False),
                                (radar_detail(managed=[{"student_id": 900, "status": "on_call_fine"}]), True)]:
            with self.subTest(expected=expected):
                session = MagicMock()
                session.get.side_effect = [response(radar_detail()), response(radar_detail()), response(state)]
                session.put.side_effect = [response({}, 400), fixed_response(403)]
                result, _ = self.run_radar(session)
                self.assertEqual(result, expected)
                self.assertEqual(session.put.call_count, 2)
                self.assertEqual(session.get.call_count, 3)

    def test_get_and_original_probe_time_reduce_later_request_budgets(self):
        session, clock = MagicMock(), [1000.0]

        def get(*args, **kwargs):
            clock[0] += 2
            return response(radar_detail())

        def put(*args, **kwargs):
            clock[0] += 3
            return response({}, 400) if session.put.call_count == 1 else fixed_response(200)

        session.get.side_effect, session.put.side_effect = get, put
        result, _ = self.run_radar(session, clock=clock)
        self.assertTrue(result)
        self.assertEqual([call.kwargs["timeout"].total for call in session.get.call_args_list], [10, 55])
        self.assertEqual([call.kwargs["timeout"].total for call in session.put.call_args_list], [8, 53])

    def test_distance_deadline_during_status_get_switches_to_fixed_with_remaining_budget(self):
        session, clock = MagicMock(), [1000.0]

        def get(*args, **kwargs):
            if session.get.call_count == 1:
                clock[0] = 1010.0
            return response(radar_detail())

        session.get.side_effect = get
        session.put.return_value = fixed_response(200)
        result, events = self.run_radar(session, clock=clock)
        self.assertTrue(result)
        self.assertEqual([call.kwargs["timeout"].total for call in session.get.call_args_list], [10, 50])
        self.assert_fixed_calls(session, 0, count=1)
        self.assertEqual(session.put.call_args.kwargs["timeout"].total, 50)
        self.assert_event(events, event="fallback_start", reason="distance_timeout")

    def test_distance_only_reports_failure_or_deadline_without_submitting_fixed_points(self):
        for elapsed, reason in [(0.25, "distance_method_failed"), (10.0, "distance_timeout")]:
            with self.subTest(elapsed=elapsed):
                session, clock = MagicMock(), [1000.0]
                end_time = "2099-01-01T00:00:00Z"

                def put(*args, **kwargs):
                    clock[0] += elapsed
                    return response({}, 400)

                session.put.side_effect = put
                with self.assertRaises(_RadarFallback) as caught:
                    self.run_radar(session, clock=clock, state=radar_detail(end_time=end_time),
                                   stage="distance", deadline=1010.0)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception.end_time, end_time)
                session.put.assert_called_once()
                self.assertEqual(session.put.call_args.kwargs["json"]["latitude"], 24.3)

    def test_fallback_only_starts_at_first_center_without_repeating_distance(self):
        session = MagicMock()
        session.put.return_value = fixed_response(200)
        result, events = self.run_radar(session, stage="fallback", deadline=1050,
                                        fallback_reason="distance_timeout")
        self.assertTrue(result)
        self.assert_fixed_calls(session, 0, count=1)
        self.assertEqual(session.get.call_args.kwargs["timeout"].total, 50)
        self.assert_event(events, event="fallback_start", reason="distance_timeout")

    def test_total_deadline_before_distance_limit_never_falls_back(self):
        session, clock = MagicMock(), [1000.0]

        def put(*args, **kwargs):
            clock[0] = 1003.0
            return response({}, 400)

        session.put.side_effect = put
        result, events = self.run_radar(session, clock=clock, deadline=1003.0)
        self.assertFalse(result)
        session.put.assert_called_once()
        self.assertFalse(any(event["event"] == "fallback_start" for event in events))

    def test_deadline_before_start_or_during_status_get_prevents_put(self):
        session = MagicMock()
        result, _ = self.run_radar(session, deadline=1000)
        self.assertFalse(result)
        session.get.assert_not_called()
        session.put.assert_not_called()

        session, clock = MagicMock(), [1000.0]

        def slow_get(*args, **kwargs):
            clock[0] = 1060
            return response(radar_detail())

        session.get.side_effect = slow_get
        result, _ = self.run_radar(session, clock=clock)
        self.assertFalse(result)
        session.get.assert_called_once()
        session.put.assert_not_called()

    def test_original_probe_exhausting_budget_never_starts_fixed_requests(self):
        session, clock = MagicMock(), [1000.0]

        def slow_put(*args, **kwargs):
            clock[0] = 1060
            return response({}, 400)

        session.put.side_effect = slow_put
        result, _ = self.run_radar(session, clock=clock)
        self.assertFalse(result)
        session.get.assert_called_once()
        session.put.assert_called_once()

    def test_round_pause_uses_remaining_budget_and_does_not_start_round_two(self):
        session, clock = MagicMock(), [1000.0]

        def put(*args, **kwargs):
            if session.put.call_count == 1:
                clock[0] = 1059.75
                return response({}, 400)
            return fixed_response(403)

        session.put.side_effect = put
        result, events = self.run_radar(session, clock=clock)
        self.assertFalse(result)
        self.assertEqual(session.put.call_count, 16)
        self.assertEqual(clock[0], 1060)
        self.assertFalse(any(event.get("round") == 2 and event["event"] == "http_response" for event in events))

    def test_known_end_time_in_the_past_never_submits_even_if_get_says_active(self):
        session = MagicMock()
        result, _ = self.run_radar(session, end_time="2000-01-01T00:00:00Z")
        self.assertFalse(result)
        session.put.assert_not_called()

    def test_radar_below_attendance_threshold_never_submits_even_with_fallback(self):
        for attended, total in [(9, 20), (10, 34)]:
            with self.subTest(attended=attended, total=total):
                session = session_for([], radar_record=radar_detail(attended, total))
                notifier = MagicMock()
                with patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = handle_rollcalls({"rollcalls": [detected(is_radar=True, is_number=False)]},
                                              session, ACCOUNT, AttendanceGuard([1]))
                self.assertEqual(result, [False])
                session.put.assert_not_called()
                notifier.send_rollcall_success.assert_not_called()


if __name__ == "__main__":
    unittest.main()
