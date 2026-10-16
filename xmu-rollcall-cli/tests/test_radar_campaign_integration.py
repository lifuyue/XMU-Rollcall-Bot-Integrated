"""Real spawned Radar workers against a loopback-only HTTP server."""

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from attendance_fixtures import radar_detail
from xmu_rollcall import radar_campaign as campaign


REAL_WORKER = campaign._campaign_worker


def local_campaign_worker(connection, settings, *args):
    """Only replace the endpoint; execute the real worker, Session and HTTP stack."""
    from xmu_rollcall import verify

    origin = settings["headers"]["X-Offline-Test-Origin"]
    if not origin.startswith("http://127.0.0.1:"):
        raise AssertionError("Integration tests must use loopback")
    verify.base_url = origin
    original_request = requests.Session.request

    def local_request(session, method, url, **kwargs):
        if not url.startswith(origin + "/"):
            raise AssertionError("External requests are forbidden in this test")
        return original_request(session, method, url, **kwargs)

    with patch.object(requests.Session, "request", local_request):
        REAL_WORKER(connection, settings, *args)


class RadarCampaignIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.object(campaign, "CONFIG_DIR", Path(self.directory.name)))
        self.enterContext(patch.dict(os.environ, {"XMU_ROLLCALL_CONFIG_DIR": self.directory.name}))
        self.requests = []
        self.request_times = []
        self.request_lock = threading.Lock()
        self.slow = False
        self.slow_gets = set()
        self.slow_puts = set()
        self.details = {}
        self.put_responses = {}
        self.put_cookies = {}
        self.release = threading.Event()
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def record(self, method, payload=None):
                with test.request_lock:
                    test.requests.append((method, self.path, dict(self.headers), payload))
                    test.request_times.append(time.monotonic())
                    return sum(item[0] == method for item in test.requests)

            def respond(self, status, data, slow=False, cookie=None):
                payload = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if cookie is not None:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                try:
                    if slow:
                        # Continuous bytes prevent an inactivity timeout; the process deadline must stop it.
                        for byte in payload:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            if test.release.wait(0.02):
                                break
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                index = self.record("GET")
                self.respond(200, test.details.get(index, radar_detail()),
                             slow=test.slow or index in test.slow_gets)

            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                index = self.record("PUT", payload)
                status, data = test.put_responses.get(index, (200, {}))
                self.respond(status, data, slow=index in test.slow_puts,
                             cookie=test.put_cookies.get(index))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop_server():
            self.release.set()
            server.shutdown()
            thread.join(2)
            server.server_close()

        self.addCleanup(stop_server)
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.session.trust_env = False
        self.session.headers["X-Offline-Test-Origin"] = f"http://127.0.0.1:{server.server_port}"
        self.session.headers["X-Test-Session"] = "synthetic-session-header"
        self.session.cookies.set("session", "synthetic-cookie", domain="127.0.0.1", path="/api")
        self.session.cookies.set("other_path", "must-not-be-sent", domain="127.0.0.1", path="/elsewhere")

    def run_campaign(self, seconds, distance_seconds=10):
        with patch.object(campaign, "CAMPAIGN_SECONDS", seconds), \
                patch.object(campaign, "DISTANCE_SECONDS", distance_seconds), \
                patch.object(campaign, "_campaign_worker", local_campaign_worker), \
                contextlib.redirect_stdout(io.StringIO()):
            return campaign.run_radar_campaign(self.session, 123, account_id=1, student_id=900, course_id=42)

    def state(self):
        return json.loads((Path(self.directory.name) / "radar_attempts.json").read_text())

    def assert_no_restart_or_children(self, before, expected_success):
        count = len(self.requests)
        self.assertEqual(self.run_campaign(5), expected_success)
        self.assertEqual(len(self.requests), count)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def submitted_points(self):
        return [(payload["latitude"], payload["longitude"])
                for method, _, _, payload in self.requests if method == "PUT"]

    def test_real_worker_preserves_cookie_headers_submits_and_persists_success(self):
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertTrue(self.run_campaign(5))
        self.assertEqual([(method, path) for method, path, _, _ in self.requests],
                         [("GET", "/api/rollcall/123"), ("PUT", "/api/rollcall/123/answer")])
        for _, _, headers, _ in self.requests:
            self.assertEqual(headers["X-Test-Session"], "synthetic-session-header")
            self.assertEqual(headers["Cookie"], "session=synthetic-cookie")
        self.assertEqual(self.requests[1][3]["latitude"], 24.3)
        self.assertEqual(self.requests[1][3]["longitude"], 118.0)
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "success")
        self.assertTrue(self.run_campaign(5))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_streaming_distance_get_is_killed_then_fixed_location_succeeds(self):
        self.slow_gets = {1}
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        self.assertTrue(self.run_campaign(4, distance_seconds=0.8))
        self.assertGreaterEqual(time.monotonic() - began, 0.65)
        self.assertLess(time.monotonic() - began, 3)
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "GET", "PUT"])
        self.assertEqual(self.submitted_points(), [(24.6082, 118.2953)])
        self.assert_no_restart_or_children(before, True)

    def test_streaming_distance_put_is_killed_then_fixed_location_succeeds(self):
        self.slow_puts = {1}
        self.put_responses[1] = (400, {"message": "synthetic slow response " * 100})
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertTrue(self.run_campaign(4, distance_seconds=0.8))
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "PUT", "GET", "PUT"])
        self.assertEqual(self.submitted_points(), [(24.3, 118.0), (24.6082, 118.2953)])
        self.assert_no_restart_or_children(before, True)

    def test_fallback_keeps_original_total_deadline_after_distance_timeout(self):
        self.slow = True
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        self.assertFalse(self.run_campaign(2.4, distance_seconds=1))
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 2.2)
        # A restarted 2.4-second total budget would finish after 3.4 seconds.
        self.assertLess(elapsed, 3.2)
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "GET"])
        self.assertGreaterEqual(self.request_times[1] - began, 0.9)
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "timeout")
        self.assert_no_restart_or_children(before, False)

    def test_fallback_checks_if_timed_out_distance_put_already_succeeded(self):
        self.slow_puts = {1}
        self.put_responses[1] = (200, {"message": "synthetic slow success " * 100})
        self.details[2] = radar_detail(managed=[{"student_id": 900, "status": "on_call"}])
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertTrue(self.run_campaign(4, distance_seconds=0.8))
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "PUT", "GET"])
        self.assertEqual(self.submitted_points(), [(24.3, 118.0)])
        self.assert_no_restart_or_children(before, True)

    def test_finished_rollcall_after_distance_timeout_never_submits(self):
        self.slow_gets = {1}
        self.details[2] = radar_detail(status="finished")
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertFalse(self.run_campaign(4, distance_seconds=0.8))
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "GET"])
        self.assertEqual(self.submitted_points(), [])
        self.assert_no_restart_or_children(before, False)

    def test_normal_distance_failure_preserves_updated_session_for_fallback(self):
        self.put_responses[1] = (400, {"message": "synthetic response without distance"})
        self.put_cookies[1] = "stage_session=synthetic-updated-cookie; Path=/api"
        before = {child.pid for child in multiprocessing.active_children()}
        self.assertTrue(self.run_campaign(5))
        self.assertEqual(self.submitted_points(), [(24.3, 118.0), (24.6082, 118.2953)])
        self.assertIn("stage_session=synthetic-updated-cookie", self.requests[2][2]["Cookie"])
        self.assertIn("stage_session=synthetic-updated-cookie", self.requests[3][2]["Cookie"])
        self.assert_no_restart_or_children(before, True)

    def test_normal_failure_preserves_discovered_end_time_for_fallback(self):
        end_time = datetime.fromtimestamp(time.time() + 1.8, timezone.utc).isoformat()
        self.details[1] = radar_detail(end_time=end_time)
        self.put_responses[1] = (400, {"message": "synthetic response without distance"})
        self.slow_gets = {2}
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        self.assertFalse(self.run_campaign(5))
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 2.8)
        self.assertEqual([method for method, _, _, _ in self.requests], ["GET", "PUT", "GET"])
        self.assertEqual(self.submitted_points(), [(24.3, 118.0)])
        self.assert_no_restart_or_children(before, False)

    def test_real_streaming_get_stops_at_deadline_and_never_submits_or_restarts(self):
        self.slow = True
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        self.assertFalse(self.run_campaign(0.8))
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 0.65)
        self.assertLess(elapsed, 2.5)
        self.assertEqual([(method, path) for method, path, _, _ in self.requests],
                         [("GET", "/api/rollcall/123")])
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "timeout")
        self.assertFalse(self.run_campaign(5))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)


if __name__ == "__main__":
    unittest.main()
