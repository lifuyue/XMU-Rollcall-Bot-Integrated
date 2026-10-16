"""One durable, time-bounded Radar campaign per account and rollcall."""

import contextlib
import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time

import requests

from .config import CONFIG_DIR
from .attendance_guard import _id, _timestamp
from .verify import DISTANCE_SECONDS

try:
    import fcntl
except ImportError:  # A thread-only lock cannot enforce the cross-process limit.
    fcntl = None

CAMPAIGN_SECONDS = 60.0
_STATE_LOCK = threading.Lock()


def _log(event, account_id, rollcall_id, **details):
    print("[Radar campaign] " + json.dumps({
        "event": event, "account_id": str(account_id), "rollcall_id": str(rollcall_id),
        **details,
    }, ensure_ascii=True), flush=True)


def _key(account_id, rollcall_id):
    if account_id is None or rollcall_id is None:
        raise ValueError("campaign identity is required")
    return json.dumps([str(account_id), str(rollcall_id)], separators=(",", ":"))


@contextlib.contextmanager
def _locked_state(deadline):
    if fcntl is None:
        raise RuntimeError("persistent campaign locking requires fcntl")
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _STATE_LOCK.acquire(timeout=remaining):
        raise TimeoutError("campaign state lock timed out")
    descriptor = None
    locked = False
    try:
        directory = Path(CONFIG_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(directory / "radar_attempts.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("campaign state lock timed out")
                time.sleep(min(0.02, remaining))
        yield directory / "radar_attempts.json"
    finally:
        if descriptor is not None:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        _STATE_LOCK.release()


def _read_state(path):
    if not path.exists():
        return {"version": 1, "attempts": {}}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("attempts"), dict):
        raise ValueError("invalid campaign state")
    return state


def _write_state(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix=".radar_attempts-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(state, handle, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # The claim must survive a restart before any request is permitted.
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _claim(account_id, rollcall_id, deadline):
    key = _key(account_id, rollcall_id)
    with _locked_state(deadline) as path:
        state = _read_state(path)
        previous = state["attempts"].get(key)
        if key in state["attempts"]:
            if not isinstance(previous, dict) or previous.get("status") not in {"started", "success", "failed", "timeout"}:
                raise ValueError("invalid campaign record")
            return False, previous["status"]
        state["attempts"][key] = {"started_at": time.time(), "status": "started"}
        _write_state(path, state)
    return True, "started"


def _finish(account_id, rollcall_id, status):
    # Cleanup may follow the network deadline; the durable claim already blocks retries.
    with _locked_state(time.monotonic() + 1.0) as path:
        state = _read_state(path)
        record = state["attempts"][_key(account_id, rollcall_id)]
        record.update(status=status, finished_at=time.time())
        _write_state(path, state)


def _session_settings(session):
    return {
        "cookies": copy.copy(session.cookies),
        "headers": dict(session.headers),
        "params": dict(session.params),
        "proxies": dict(session.proxies),
        "auth": session.auth,
        "verify": session.verify,
        "cert": session.cert,
        "trust_env": session.trust_env,
        "max_redirects": session.max_redirects,
    }


def _campaign_worker(connection, settings, rollcall_id, account_id, student_id, course_id, end_time, deadline,
                     stage="all", fallback_reason="distance_method_failed"):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        connection.close()
        return
    # Also stop if the supervising process disappears while a request is blocked.
    watchdog = threading.Timer(remaining, os._exit, args=(124,))
    watchdog.daemon = True
    watchdog.start()
    threading.current_thread().name = f"xmu-account-{account_id}"
    try:
        from .network import configure_network
        from .verify import send_radar, _RadarFallback
        configure_network()
        with requests.Session() as session:
            for key, value in settings.items():
                setattr(session, key, value)
            try:
                result = send_radar(
                    session, rollcall_id, student_id=student_id, course_id=course_id,
                    end_time=end_time, deadline=deadline, stage=stage, fallback_reason=fallback_reason,
                )
            except _RadarFallback as fallback:
                # Carry cookies and any newly learned end time into the next process.
                result = {"status": "fallback", "settings": _session_settings(session),
                          "end_time": fallback.end_time, "reason": fallback.reason}
        connection.send(result)
    except Exception as exc:
        _log("worker_failed", account_id, rollcall_id, exception_type=type(exc).__name__)
        try:
            connection.send(False)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        watchdog.cancel()
        connection.close()


def _stop_process(process):
    if process.is_alive():
        process.terminate()
        # Do not spend more of the expired budget waiting for a cooperative exit.
        if process.is_alive():
            process.kill()
    process.join(timeout=1.0)
    if process.is_alive():
        raise RuntimeError("campaign process did not stop")


def _run_stage(settings, rollcall_id, account_id, student_id, course_id, end_time, deadline,
               stage, fallback_reason="distance_method_failed"):
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_campaign_worker,
        args=(sending, settings, rollcall_id, account_id, student_id, course_id, end_time, deadline,
              stage, fallback_reason),
        daemon=True,
    )
    started = False
    try:
        if time.monotonic() >= deadline:
            return "timeout"
        process.start()
        started = True
        sending.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receiving.poll(remaining):
            return "timeout"
        try:
            result = receiving.recv()
        except EOFError:
            process.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            return "timeout" if process.exitcode == 124 or time.monotonic() >= deadline else "failed"
        if time.monotonic() >= deadline:
            return "timeout"
        if stage == "distance" and isinstance(result, dict) and result.get("status") == "fallback":
            return result
        return "success" if result is True else "failed"
    finally:
        try:
            if started:
                _stop_process(process)
        finally:
            receiving.close()
            sending.close()
            if not started or not process.is_alive():
                process.close()


def _run_process(settings, rollcall_id, account_id, student_id, course_id, end_time, deadline):
    distance_deadline = min(deadline, time.monotonic() + DISTANCE_SECONDS)
    _log("stage_start", account_id, rollcall_id, stage="distance")
    result = _run_stage(settings, rollcall_id, account_id, student_id, course_id, end_time,
                        distance_deadline, "distance")
    if isinstance(result, dict):
        settings, end_time = result["settings"], result["end_time"]
        reason = result["reason"]
        if end_time is not None:
            deadline = min(deadline, time.monotonic() + _timestamp(end_time).timestamp() - time.time())
    elif result == "timeout" and distance_deadline < deadline:
        reason = "distance_timeout"
    else:
        return result
    # _run_stage has already killed/reaped the old process. Never extend the total budget.
    if time.monotonic() >= deadline:
        return "timeout"
    _log("stage_start", account_id, rollcall_id, stage="fallback", reason=reason)
    return _run_stage(settings, rollcall_id, account_id, student_id, course_id, end_time,
                      deadline, "fallback", reason)


def run_radar_campaign(session, rollcall_id, *, account_id, student_id, course_id, end_time=None):
    """Claim once, then run the whole campaign under a shared 60-second deadline.

    A claimed campaign never restarts, including after crashes or service restarts.
    Platforms without fcntl fail closed instead of weakening the attempt limit.
    """
    deadline = time.monotonic() + CAMPAIGN_SECONDS
    try:
        account_id, rollcall_id, student_id, course_id = map(
            _id, (account_id, rollcall_id, student_id, course_id),
        )
        if end_time is not None:
            remaining = _timestamp(end_time).timestamp() - time.time()
            deadline = min(deadline, time.monotonic() + remaining)
    except (ValueError, TypeError, OverflowError):
        _log("invalid_context", "invalid", "invalid")
        return False
    if deadline <= time.monotonic():
        _log("finished", account_id, rollcall_id, status="timeout", reason="rollcall_ended")
        return False
    try:
        claimed, previous = _claim(account_id, rollcall_id, deadline)
    except Exception as exc:
        _log("state_unavailable", account_id, rollcall_id, exception_type=type(exc).__name__,
             reason="persistent_lock_unavailable" if fcntl is None else "claim_failed")
        return False
    if not claimed:
        _log("already_claimed", account_id, rollcall_id, status=previous)
        return previous == "success"
    try:
        status = _run_process(
            _session_settings(session), rollcall_id, account_id, student_id, course_id, end_time, deadline,
        )
    except Exception as exc:
        status = "timeout" if time.monotonic() >= deadline else "failed"
        _log("execution_failed", account_id, rollcall_id, exception_type=type(exc).__name__)
    try:
        _finish(account_id, rollcall_id, status)
    except Exception as exc:
        _log("state_update_failed", account_id, rollcall_id, exception_type=type(exc).__name__)
    _log("finished", account_id, rollcall_id, status=status)
    return status == "success"
