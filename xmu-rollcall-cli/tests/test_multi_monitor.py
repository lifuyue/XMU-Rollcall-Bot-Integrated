import unittest

from xmu_rollcall.multi_monitor import MultiAccountMonitor, is_account_complete


class MultiAccountMonitorTest(unittest.TestCase):
    def test_filters_incomplete_accounts(self):
        monitor = MultiAccountMonitor(
            [
                {"id": 1, "username": "a", "password": "p"},
                {"id": 2, "username": "b", "password": ""},
                {"id": 3, "username": "", "password": "p"},
            ],
            poll_interval=1,
        )

        self.assertEqual([account["id"] for account in monitor.accounts], [1])

    def test_poll_interval_is_clamped_to_one_second(self):
        monitor = MultiAccountMonitor(
            [{"id": 1, "username": "a", "password": "p"}],
            poll_interval=0.1,
        )

        self.assertEqual(monitor.poll_interval, 1.0)

    def test_stagger_delay_spreads_account_startup(self):
        monitor = MultiAccountMonitor(
            [
                {"id": 1, "username": "a", "password": "p"},
                {"id": 2, "username": "b", "password": "p"},
                {"id": 3, "username": "c", "password": "p"},
            ],
            poll_interval=3,
        )

        self.assertEqual(monitor._stagger_delay(0), 0)
        self.assertEqual(monitor._stagger_delay(1), 1)
        self.assertEqual(monitor._stagger_delay(2), 2)

    def test_account_complete_requires_username_and_password(self):
        self.assertTrue(is_account_complete({"username": "a", "password": "p"}))
        self.assertFalse(is_account_complete({"username": "a", "password": ""}))
        self.assertFalse(is_account_complete({"username": "", "password": "p"}))


if __name__ == "__main__":
    unittest.main()
