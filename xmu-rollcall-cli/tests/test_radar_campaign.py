"""Campaign limits use synthetic sessions and child processes; no school requests."""

import contextlib
from datetime import datetime, timezone
import io
import json
import multiprocessing
import os
import runpy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from xmu_rollcall import radar_campaign as campaign


SECRET = "synthetic-private-cookie-token"


def slow_worker(connection, *args):
    time.sleep(5)
    connection.send(True)
    connection.close()


def blocked_verification(*args, **kwargs):
    time.sleep(5)
    return True


def self_timed_worker(connection, *args):
    from xmu_rollcall import verify
    verify.send_radar = blocked_verification
    campaign._campaign_worker(connection, *args)


def claim_from_another_process(connection, directory):
    campaign.CONFIG_DIR = Path(directory)
    connection.send(campaign._claim(1, 123, time.monotonic() + 2))
    connection.close()


class RadarCampaignTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = patch.object(campaign, "CONFIG_DIR", Path(self.directory.name))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.session = requests.Session()
        self.session.cookies.set("session", SECRET, domain="lnt.xmu.edu.cn", path="/api")
        self.session.headers["Authorization"] = SECRET
        self.session.auth = ("synthetic-user", SECRET)
        self.addCleanup(self.session.close)

    def run_campaign(self, session=None, account_id=1):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = campaign.run_radar_campaign(
                self.session if session is None else session, 123,
                account_id=account_id, student_id=900, course_id=42,
            )
        self.assertNotIn(SECRET, output.getvalue())
        return result

    def state(self):
        return json.loads((Path(self.directory.name) / "radar_attempts.json").read_text())

    def test_failed_campaign_is_not_restarted_with_another_session_or_process(self):
        with patch.object(campaign, "_run_process", return_value="failed") as run:
            self.assertFalse(self.run_campaign())
            with requests.Session() as another:
                self.assertFalse(self.run_campaign(another))
            run.assert_called_once()
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        child = context.Process(target=claim_from_another_process, args=(sending, self.directory.name))
        try:
            child.start()
            sending.close()
            self.assertTrue(receiving.poll(5))
            self.assertEqual(receiving.recv(), (False, "failed"))
            child.join(2)
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.kill()
                child.join(2)
            receiving.close()
            sending.close()
            child.close()

    def test_two_processes_cannot_claim_the_same_campaign(self):
        context = multiprocessing.get_context("spawn")
        children = []
        for _ in range(2):
            receiving, sending = context.Pipe(duplex=False)
            child = context.Process(target=claim_from_another_process, args=(sending, self.directory.name))
            children.append((child, receiving, sending))
        try:
            for child, _, sending in children:
                child.start()
                sending.close()
            results = []
            for child, receiving, _ in children:
                self.assertTrue(receiving.poll(5))
                results.append(receiving.recv())
                child.join(2)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sorted(results), [(False, "started"), (True, "started")])
        finally:
            for child, receiving, sending in children:
                if child.is_alive():
                    child.kill()
                    child.join(2)
                receiving.close()
                sending.close()
                child.close()

    def test_started_record_after_interruption_never_restarts(self):
        self.assertEqual(campaign._claim(1, 123, time.monotonic() + 1), (True, "started"))
        with patch.object(campaign, "_run_process") as run:
            self.assertFalse(self.run_campaign())
            run.assert_not_called()
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "started")

    def test_success_reuses_result_and_other_accounts_are_independent(self):
        with patch.object(campaign, "_run_process", return_value="success") as run:
            self.assertTrue(self.run_campaign())
            self.assertTrue(self.run_campaign())
            self.assertTrue(self.run_campaign(account_id=2))
            self.assertEqual(run.call_count, 2)
        entries = self.state()["attempts"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(record["status"] == "success" for record in entries.values()))
        self.assertEqual(os.stat(Path(self.directory.name) / "radar_attempts.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(self.directory.name) / "radar_attempts.lock").st_mode & 0o777, 0o600)

    def test_identity_and_end_time_are_validated_before_claiming(self):
        base = dict(account_id=1, student_id=900, course_id=42)
        for field, value in [("account_id", None), ("student_id", SECRET), ("course_id", True),
                             ("end_time", "not-a-timestamp")]:
            with self.subTest(field=field):
                kwargs = dict(base, **{field: value})
                with patch.object(campaign, "_claim") as claim, patch.object(campaign, "_run_process") as run, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertFalse(campaign.run_radar_campaign(self.session, 123, **kwargs))
                claim.assert_not_called()
                run.assert_not_called()
                self.assertNotIn(SECRET, output.getvalue())
        with patch.object(campaign, "_claim") as claim, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(campaign.run_radar_campaign(self.session, None, **base))
        claim.assert_not_called()

    def test_known_end_time_shortens_the_original_campaign_deadline(self):
        with patch.object(campaign.time, "monotonic", return_value=1000.0), \
                patch.object(campaign.time, "time", return_value=2000.0), \
                patch.object(campaign, "_run_process", return_value="failed") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(campaign.run_radar_campaign(
                self.session, 123, account_id=1, student_id=900, course_id=42,
                end_time=datetime.fromtimestamp(2007.0, timezone.utc).isoformat(),
            ))
        self.assertEqual(run.call_args.args[-1], 1007.0)

    def test_expired_rollcall_does_not_claim_or_start(self):
        with patch.object(campaign, "_claim") as claim, patch.object(campaign, "_run_process") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(campaign.run_radar_campaign(
                self.session, 123, account_id=1, student_id=900, course_id=42,
                end_time="2020-01-01T00:00:00Z",
            ))
        claim.assert_not_called()
        run.assert_not_called()

    def test_spawn_bootstrap_does_not_run_the_cli_command(self):
        with patch("click.core.Command.__call__", side_effect=AssertionError("CLI invoked during spawn")) as command:
            runpy.run_module("xmu_rollcall.cli", run_name="__mp_main__")
        command.assert_not_called()

    def test_claim_write_failure_does_not_start_a_process_or_leak_error(self):
        with patch.object(campaign, "_write_state", side_effect=OSError(SECRET)), \
                patch.object(campaign, "_run_process") as run:
            self.assertFalse(self.run_campaign())
            run.assert_not_called()

    def test_invalid_state_and_unavailable_file_lock_fail_closed(self):
        path = Path(self.directory.name) / "radar_attempts.json"
        path.write_text(json.dumps({"version": 1, "attempts": {campaign._key(1, 123): None}}))
        with patch.object(campaign, "_run_process") as run:
            self.assertFalse(self.run_campaign())
            run.assert_not_called()
        path.unlink()
        with patch.object(campaign, "fcntl", None), patch.object(campaign, "_run_process") as run:
            self.assertFalse(self.run_campaign())
            run.assert_not_called()

    def fake_context(self, *, poll=False, result=False, eof=False):
        context = MagicMock()
        receiving, sending, process = MagicMock(), MagicMock(), MagicMock()
        context.Pipe.return_value = (receiving, sending)
        context.Process.return_value = process
        receiving.poll.return_value = poll
        receiving.recv.side_effect = EOFError() if eof else None
        receiving.recv.return_value = result
        process.is_alive.side_effect = [True, True, False, False]
        return context, receiving, sending, process

    def test_timeout_terminates_then_kills_and_reaps_child(self):
        context, receiving, sending, process = self.fake_context()
        with patch.object(campaign.multiprocessing, "get_context", return_value=context) as factory:
            self.assertEqual(campaign._run_stage({}, 123, 1, 900, 42, None, time.monotonic() + 1, "distance"), "timeout")
        factory.assert_called_once_with("spawn")
        context.Pipe.assert_called_once_with(duplex=False)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        process.join.assert_called_once_with(timeout=1.0)
        process.close.assert_called_once()
        receiving.close.assert_called_once()
        self.assertEqual(sending.close.call_count, 2)

    def test_eof_from_crashed_child_is_failure_not_a_busy_wait(self):
        context, receiving, _, process = self.fake_context(poll=True, eof=True)
        with patch.object(campaign.multiprocessing, "get_context", return_value=context):
            self.assertEqual(campaign._run_stage({}, 123, 1, 900, 42, None, time.monotonic() + 1, "distance"), "failed")
        receiving.poll.assert_called_once()
        process.join.assert_any_call(timeout=1.0)

    def test_watchdog_exit_is_timeout_even_if_pipe_eof_arrives_first(self):
        context, _, _, process = self.fake_context(poll=True, eof=True)
        process.exitcode = 124
        with patch.object(campaign.multiprocessing, "get_context", return_value=context):
            self.assertEqual(campaign._run_stage({}, 123, 1, 900, 42, None, time.monotonic() + 1, "distance"), "timeout")

    def test_normal_distance_fallback_carries_settings_and_only_shortens_total_deadline(self):
        initial = {"cookies": "original-synthetic-cookie"}
        updated = {"cookies": SECRET, "headers": {"X-Stage": "updated"}}
        for end_epoch, expected_deadline in [(2030.0, 1030.0), (2200.0, 1060.0)]:
            with self.subTest(end_epoch=end_epoch):
                end_time = datetime.fromtimestamp(end_epoch, timezone.utc).isoformat()
                message = {"status": "fallback", "settings": updated, "end_time": end_time,
                           "reason": "distance_method_failed"}
                with patch.object(campaign.time, "monotonic", return_value=1000.0), \
                        patch.object(campaign.time, "time", return_value=2000.0), \
                        patch.object(campaign, "_run_stage", side_effect=[message, "success"]) as stage, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    result = campaign._run_process(initial, 123, 1, 900, 42, None, 1060.0)
                self.assertEqual(result, "success")
                self.assertEqual(stage.call_count, 2)
                self.assertEqual(stage.call_args_list[0].args,
                                 (initial, 123, 1, 900, 42, None, 1010.0, "distance"))
                self.assertEqual(stage.call_args_list[1].args,
                                 (updated, 123, 1, 900, 42, end_time, expected_deadline,
                                  "fallback", "distance_method_failed"))
                self.assertNotIn(SECRET, output.getvalue())

    def test_distance_timeout_runs_fallback_with_original_remaining_budget(self):
        clock = [1000.0]
        initial = {"cookies": SECRET}
        def run_stage(*args):
            if args[7] == "distance":
                self.assertEqual(args[6], 1010.0)
                clock[0] = 1010.0
                return "timeout"
            self.assertEqual(args, (initial, 123, 1, 900, 42, None, 1060.0,
                                    "fallback", "distance_timeout"))
            return "success"
        with patch.object(campaign.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(campaign, "_run_stage", side_effect=run_stage) as stage, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(campaign._run_process(initial, 123, 1, 900, 42, None, 1060.0), "success")
        self.assertEqual(stage.call_count, 2)
        self.assertNotIn(SECRET, output.getvalue())

    def test_total_deadline_before_ten_seconds_prevents_fallback(self):
        clock = [1000.0]
        def run_stage(*args):
            self.assertEqual(args[6], 1005.0)
            clock[0] = 1005.0
            return "timeout"
        with patch.object(campaign.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(campaign, "_run_stage", side_effect=run_stage) as stage, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(campaign._run_process({}, 123, 1, 900, 42, None, 1005.0), "timeout")
        stage.assert_called_once()

    def test_total_deadline_during_distance_cleanup_prevents_fallback(self):
        clock = [1000.0]
        def run_stage(*args):
            clock[0] = 1060.1
            return {"status": "fallback", "settings": {}, "end_time": None,
                    "reason": "distance_method_failed"}
        with patch.object(campaign.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(campaign, "_run_stage", side_effect=run_stage) as stage, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(campaign._run_process({}, 123, 1, 900, 42, None, 1060.0), "timeout")
        stage.assert_called_once()

    def test_distance_success_or_terminal_failure_never_starts_fallback(self):
        for result in ("success", "failed"):
            with self.subTest(result=result), \
                    patch.object(campaign, "_run_stage", return_value=result) as stage, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(campaign._run_process({}, 123, 1, 900, 42, None,
                                                       time.monotonic() + 60), result)
            stage.assert_called_once()

    def test_two_stages_share_one_claim_and_one_terminal_record(self):
        message = {"status": "fallback", "settings": campaign._session_settings(self.session),
                   "end_time": None, "reason": "distance_method_failed"}
        with patch.object(campaign, "_claim", wraps=campaign._claim) as claim, \
                patch.object(campaign, "_finish", wraps=campaign._finish) as finish, \
                patch.object(campaign, "_run_stage", side_effect=[message, "success"]) as stage:
            self.assertTrue(self.run_campaign())
            claim.assert_called_once()
            finish.assert_called_once_with("1", "123", "success")
            self.assertEqual(stage.call_count, 2)
            self.assertTrue(self.run_campaign())
            self.assertEqual(stage.call_count, 2)
            finish.assert_called_once()
        entries = self.state()["attempts"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[campaign._key(1, 123)]["status"], "success")

    def test_worker_returns_updated_cookies_and_end_time_for_normal_fallback(self):
        from xmu_rollcall.verify import _RadarFallback
        connection = MagicMock()
        deadline = time.monotonic() + 2
        end_time = "2030-01-01T00:00:00Z"
        def distance_failed(session, rollcall_id, **kwargs):
            self.assertEqual(kwargs["stage"], "distance")
            session.cookies.set("session", "updated-" + SECRET, domain="lnt.xmu.edu.cn", path="/api")
            session.headers["X-Stage"] = "updated"
            raise _RadarFallback("distance_method_failed", end_time)
        previous_name = threading.current_thread().name
        try:
            with patch("xmu_rollcall.verify.send_radar", side_effect=distance_failed), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                campaign._campaign_worker(connection, campaign._session_settings(self.session),
                                          123, 1, 900, 42, None, deadline, "distance")
        finally:
            threading.current_thread().name = previous_name
        connection.send.assert_called_once()
        message = connection.send.call_args.args[0]
        self.assertEqual(message["status"], "fallback")
        self.assertEqual(message["reason"], "distance_method_failed")
        self.assertEqual(message["end_time"], end_time)
        self.assertEqual(message["settings"]["cookies"].get("session", domain="lnt.xmu.edu.cn", path="/api"),
                         "updated-" + SECRET)
        self.assertEqual(message["settings"]["headers"]["X-Stage"], "updated")
        self.assertNotIn(SECRET, output.getvalue())
        connection.close.assert_called_once()

    def test_real_slow_child_is_stopped_near_deadline_and_cannot_restart(self):
        before = {child.pid for child in multiprocessing.active_children()}
        began = time.monotonic()
        with patch.object(campaign, "CAMPAIGN_SECONDS", 0.35), patch.object(campaign, "_campaign_worker", slow_worker):
            self.assertFalse(self.run_campaign())
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.state()["attempts"][campaign._key(1, 123)]["status"], "timeout")
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
        with patch.object(campaign, "_run_process") as run:
            self.assertFalse(self.run_campaign())
            run.assert_not_called()

    def test_worker_watchdog_stops_blocked_work_without_parent_termination(self):
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        child = context.Process(
            target=self_timed_worker,
            args=(sending, {}, 123, 1, 900, 42, None, time.monotonic() + 0.5),
        )
        began = time.monotonic()
        try:
            child.start()
            sending.close()
            child.join(2)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 124)
            self.assertLess(time.monotonic() - began, 2.0)
        finally:
            if child.is_alive():
                child.kill()
                child.join(2)
            receiving.close()
            sending.close()
            child.close()

    def test_worker_copies_session_and_passes_the_original_deadline_without_outputting_secrets(self):
        connection = MagicMock()
        settings = campaign._session_settings(self.session)
        deadline = time.monotonic() + 2
        previous_name = threading.current_thread().name
        try:
            with patch("xmu_rollcall.verify.send_radar", return_value=True) as radar, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                campaign._campaign_worker(connection, settings, 123, 1, 900, 42, None, deadline)
        finally:
            threading.current_thread().name = previous_name
        self.assertNotIn(SECRET, output.getvalue())
        copied = radar.call_args.args[0]
        self.assertIsNot(copied, self.session)
        self.assertIsNot(copied.cookies, self.session.cookies)
        self.assertEqual(copied.headers["Authorization"], SECRET)
        cookie = next(iter(copied.cookies))
        self.assertEqual((cookie.value, cookie.domain, cookie.path), (SECRET, "lnt.xmu.edu.cn", "/api"))
        self.assertEqual(copied.auth, self.session.auth)
        self.assertEqual(radar.call_args.kwargs, dict(student_id=900, course_id=42, end_time=None, deadline=deadline,
                                                       stage="all", fallback_reason="distance_method_failed"))
        connection.send.assert_called_once_with(True)
        connection.close.assert_called_once()

    def test_worker_failure_reports_only_exception_type(self):
        connection = MagicMock()
        previous_name = threading.current_thread().name
        try:
            with patch("xmu_rollcall.verify.send_radar", side_effect=requests.Timeout(SECRET)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                campaign._campaign_worker(connection, campaign._session_settings(self.session), 123, 1, 900, 42, None, time.monotonic() + 2)
        finally:
            threading.current_thread().name = previous_name
        self.assertNotIn(SECRET, output.getvalue())
        self.assertIn('"exception_type": "Timeout"', output.getvalue())
        connection.send.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
