import threading
import time
from dataclasses import dataclass

import requests
from xmulogin import xmulogin

from .config import get_cookies_path
from .monitor import base_url, headers, interval
from .rollcall_handler import process_rollcalls
from .utils import load_session, save_session, verify_session


@dataclass
class AccountMonitorResult:
    account_id: int
    account_name: str
    started: bool
    message: str = ""


def account_label(account):
    return f"{account.get('name') or account.get('username')} (ID: {account.get('id')})"


def is_account_complete(account):
    return bool(account.get("username") and account.get("password"))


def authenticate_account(account, log=print):
    account_id = account.get("id", 1)
    label = account_label(account)
    cookies_path = get_cookies_path(account_id)

    session = None
    session_candidate = requests.Session()
    if load_session(session_candidate, cookies_path):
        profile = verify_session(session_candidate)
        if profile:
            session = session_candidate
            log(f"[{label}] Session restored.")
        else:
            log(f"[{label}] Cached session expired, logging in again.")

    if session is None:
        log(f"[{label}] Logging in.")
        session = xmulogin(
            type=3,
            username=account["username"],
            password=account["password"],
        )
        if session:
            save_session(session, cookies_path)
            log(f"[{label}] Login successful.")
        else:
            raise RuntimeError("login failed")

    return session


class MultiAccountMonitor:
    def __init__(self, accounts, poll_interval=interval, stagger=True, log=print):
        self.accounts = [account for account in accounts if is_account_complete(account)]
        self.poll_interval = max(float(poll_interval), 1.0)
        self.stagger = stagger
        self.log = log
        self.stop_event = threading.Event()
        self.threads = []
        self.results = []

    def start(self):
        if not self.accounts:
            raise RuntimeError("no complete accounts configured")

        self.log(
            f"Starting multi-account monitor for {len(self.accounts)} account(s). "
            f"Poll interval: {self.poll_interval:g}s per account."
        )

        for index, account in enumerate(self.accounts):
            delay = self._stagger_delay(index)
            thread = threading.Thread(
                target=self._run_account,
                args=(account, delay),
                name=f"xmu-account-{account.get('id', index + 1)}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        try:
            while any(thread.is_alive() for thread in self.threads):
                time.sleep(1)
        except KeyboardInterrupt:
            self.log("Stopping multi-account monitor...")
            self.stop()
            for thread in self.threads:
                thread.join(timeout=5)
            raise

        if self.results and not any(result.started for result in self.results):
            raise RuntimeError("all account monitors failed to start")

    def stop(self):
        self.stop_event.set()

    def _stagger_delay(self, index):
        if not self.stagger or len(self.accounts) <= 1:
            return 0
        return (self.poll_interval / len(self.accounts)) * index

    def _run_account(self, account, initial_delay):
        label = account_label(account)
        if initial_delay:
            time.sleep(initial_delay)

        try:
            session = authenticate_account(account, self.log)
        except Exception as exc:
            self.log(f"[{label}] Failed to initialize: {exc}")
            self.results.append(
                AccountMonitorResult(
                    account_id=account.get("id"),
                    account_name=label,
                    started=False,
                    message=str(exc),
                )
            )
            return

        self.results.append(
            AccountMonitorResult(
                account_id=account.get("id"),
                account_name=label,
                started=True,
            )
        )
        self.log(f"[{label}] Monitoring started.")

        rollcalls_url = f"{base_url}/api/radar/rollcalls"
        temp_data = {"rollcalls": []}
        query_count = 0
        next_poll = time.monotonic()

        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_poll:
                time.sleep(min(0.2, next_poll - now))
                continue

            next_poll = now + self.poll_interval
            try:
                response = session.get(rollcalls_url, headers=headers, timeout=20)
                response.raise_for_status()
                data = response.json()
                query_count += 1

                if temp_data != data:
                    temp_data = data
                    rollcalls = temp_data.get("rollcalls") or []
                    if rollcalls:
                        self.log(f"[{label}] New rollcall(s) found: {len(rollcalls)}")
                        temp_data = process_rollcalls(temp_data, session, account=account)
            except Exception as exc:
                self.log(f"[{label}] Poll failed: {exc}")
                next_poll = time.monotonic() + max(self.poll_interval, 5)

        self.log(f"[{label}] Monitoring stopped. Queries: {query_count}")


def start_multi_account_monitor(accounts, poll_interval=interval, log=print):
    monitor = MultiAccountMonitor(accounts, poll_interval=poll_interval, log=log)
    monitor.start()
    return monitor
