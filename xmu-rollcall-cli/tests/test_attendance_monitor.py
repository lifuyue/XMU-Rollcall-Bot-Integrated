import contextlib
import io
import itertools
import unittest
from unittest.mock import MagicMock, patch

from attendance_fixtures import ACCOUNT, detected, radar_detail, response, session_for, timetable
from xmu_rollcall.attendance_guard import AttendanceGuard
from xmu_rollcall.monitor import start_monitor
from xmu_rollcall.multi_monitor import MultiAccountMonitor
from xmu_rollcall.verify import send_radar


def direct_radar_for_test(session, rollcall_id, **kwargs):
    """Exercise the real submission logic without spawning a child or opening a session."""
    kwargs.pop("account_id", None)
    with patch("xmu_rollcall.verify.time.monotonic", return_value=1000):
        return send_radar(session, rollcall_id, **kwargs)


def polling_session(radar=False, interrupt_after=None):
    session = session_for([] if radar else None)
    data = {"rollcalls": [detected(is_radar=radar, is_number=not radar)]}
    original_get = session.get.side_effect
    polls = 0
    rosters = 0

    def get(url, **kwargs):
        nonlocal polls, rosters
        if url.endswith("/api/radar/rollcalls"):
            polls += 1
            if interrupt_after and polls > interrupt_after:
                raise KeyboardInterrupt
            return response(data)
        if radar and url.endswith("/api/rollcall/123"):
            rosters += 1
            return response(radar_detail(9 if rosters == 1 else 10))
        if not radar and url.endswith("/api/timetable_rollcalls"):
            rosters += 1
            return response({"rollcalls": [timetable(9 if rosters == 1 else 10)]})
        return original_get(url, **kwargs)

    session.get.side_effect = get
    return session


class AttendanceMonitorTest(unittest.TestCase):
    def test_multi_account_polls_unchanged_discovery_until_roster_passes(self):
        for radar in (False, True):
            with self.subTest(radar=radar), contextlib.redirect_stdout(io.StringIO()):
                session = polling_session(radar)
                with patch("xmu_rollcall.attendance_guard.load_config", return_value={}):
                    monitor = MultiAccountMonitor([ACCOUNT], poll_interval=1, log=lambda *_: None)
                monitor.stop_event = MagicMock()
                monitor.stop_event.is_set.side_effect = [False, False, False, True]
                clock = itertools.count(step=10)
                with patch("xmu_rollcall.multi_monitor.authenticate_account", return_value=session), \
                        patch("xmu_rollcall.multi_monitor.time.monotonic", side_effect=lambda: next(clock)), \
                        patch("xmu_rollcall.rollcall_handler.send_radar", side_effect=direct_radar_for_test), \
                        patch("xmu_rollcall.rollcall_handler.build_notifier") as notifier:
                    monitor._run_account(ACCOUNT, 0)
                session.put.assert_called_once()
                self.assertTrue(session.put.call_args.args[0].endswith(
                    "/answer" if radar else "/answer_number_rollcall"))
                if not radar:
                    self.assertEqual(session.put.call_args.kwargs["json"]["numberCode"], "0034")
                notifier.return_value.send_rollcall_success.assert_called_once()
                roster_path = "/api/rollcall/123" if radar else "/api/timetable_rollcalls"
                self.assertEqual(sum(c.args[0].endswith(roster_path)
                                     for c in session.get.call_args_list), 3 if radar else 2)

    def test_handler_exception_does_not_poison_poll_cache(self):
        session = polling_session()
        with patch("xmu_rollcall.attendance_guard.load_config", return_value={}):
            monitor = MultiAccountMonitor([ACCOUNT], poll_interval=1, log=lambda *_: None)
        monitor.stop_event = MagicMock()
        monitor.stop_event.is_set.side_effect = [False, False, True]
        clock = itertools.count(step=10)
        with patch("xmu_rollcall.multi_monitor.authenticate_account", return_value=session), \
                patch("xmu_rollcall.multi_monitor.time.monotonic", side_effect=lambda: next(clock)), \
                patch("xmu_rollcall.multi_monitor.process_rollcalls", side_effect=[RuntimeError(), {}]) as process:
            monitor._run_account(ACCOUNT, 0)
        self.assertEqual(process.call_count, 2)

    def test_single_account_also_waits_for_the_same_threshold(self):
        session = polling_session(interrupt_after=2)
        clock = itertools.count(step=10)
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch("xmu_rollcall.monitor.AttendanceGuard.for_accounts", return_value=AttendanceGuard([1])))
            stack.enter_context(patch("xmu_rollcall.monitor.requests.Session", return_value=session))
            stack.enter_context(patch("xmu_rollcall.monitor.os.path.exists", return_value=True))
            stack.enter_context(patch("xmu_rollcall.monitor.load_session", return_value=True))
            stack.enter_context(patch("xmu_rollcall.monitor.verify_session", return_value={"id": 900}))
            stack.enter_context(patch("xmu_rollcall.monitor.time.time", side_effect=lambda: next(clock)))
            stack.enter_context(patch("xmu_rollcall.monitor.time.sleep"))
            for name in ("clear_screen", "print_banner", "print_dashboard", "update_status_line", "update_footer_text"):
                stack.enter_context(patch("xmu_rollcall.monitor." + name))
            notifier = stack.enter_context(patch("xmu_rollcall.rollcall_handler.build_notifier"))
            with self.assertRaises(SystemExit) as stopped:
                start_monitor(ACCOUNT)
        self.assertEqual(stopped.exception.code, 0)
        session.put.assert_called_once()
        notifier.return_value.send_rollcall_success.assert_called_once()


if __name__ == "__main__":
    unittest.main()
