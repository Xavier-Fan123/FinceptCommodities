import threading
import unittest

from lpg.refresh_jobs import RefreshBusy, RefreshJobManager


class RefreshJobTests(unittest.TestCase):
    @staticmethod
    def wait_for_terminal(jobs, job_id):
        for _ in range(200):
            current = jobs.get(job_id)
            if current["state"] not in {"queued", "running"}:
                return current
            threading.Event().wait(0.005)
        raise AssertionError(f"job {job_id} did not reach a terminal state")

    def test_single_flight_and_completion(self):
        release = threading.Event()

        def runner(scope):
            release.wait(2)
            return {"scope": scope, "state": "succeeded", "count": 3}

        jobs = RefreshJobManager(runner)
        first = jobs.start("asia")
        with self.assertRaises(RefreshBusy):
            jobs.start("all")
        release.set()
        current = self.wait_for_terminal(jobs, first["id"])
        self.assertEqual("succeeded", current["state"])
        self.assertEqual(3, current["result"]["count"])
        self.assertIsNotNone(current["started_at"])
        self.assertIsNotNone(current["finished_at"])
        self.assertIsNone(jobs.active())

    def test_runner_supported_terminal_states_are_preserved(self):
        for terminal in ("succeeded", "deferred", "partial", "failed", "blocked"):
            with self.subTest(terminal=terminal):
                jobs = RefreshJobManager(lambda scope, state=terminal: {"state": state, "scope": scope})
                started = jobs.start("news")
                current = self.wait_for_terminal(jobs, started["id"])
                self.assertEqual(terminal, current["state"])
                self.assertEqual(terminal, current["result"]["state"])
                if terminal in {"failed", "blocked"}:
                    self.assertEqual(terminal, current["error"])
                else:
                    self.assertIsNone(current["error"])

    def test_runner_exception_becomes_failed_terminal_state(self):
        def fail(_scope):
            raise RuntimeError("offline fixture failure")

        jobs = RefreshJobManager(fail)
        started = jobs.start("overnight")
        current = self.wait_for_terminal(jobs, started["id"])
        self.assertEqual("failed", current["state"])
        self.assertEqual("offline fixture failure", current["error"])
        self.assertIsNone(current["result"])
        self.assertIsNone(jobs.active())

    def test_unknown_runner_state_is_normalized_to_failed(self):
        jobs = RefreshJobManager(lambda _scope: {"state": "surprising"})
        started = jobs.start("asia")
        current = self.wait_for_terminal(jobs, started["id"])
        self.assertEqual("failed", current["state"])
        self.assertEqual("surprising", current["result"]["state"])
        self.assertEqual("failed", current["error"])

    def test_invalid_scope(self):
        jobs = RefreshJobManager(lambda scope: {})
        with self.assertRaises(ValueError):
            jobs.start("everything")

    def test_specialized_data_scopes_are_supported(self):
        for scope in ("history", "curves", "moc"):
            with self.subTest(scope=scope):
                jobs = RefreshJobManager(
                    lambda selected: {"state": "succeeded", "scope": selected}
                )
                started = jobs.start(scope)
                current = self.wait_for_terminal(jobs, started["id"])
                self.assertEqual("succeeded", current["state"])
                self.assertEqual(scope, current["result"]["scope"])

    def test_targeted_parameters_are_forwarded_and_visible_on_job(self):
        received = {}

        def runner(scope, **parameters):
            received.update(parameters)
            return {"state": "succeeded", "scope": scope, "parameters": parameters}

        jobs = RefreshJobManager(runner)
        started = jobs.start(
            "history",
            parameters={"symbols": ["PMAAV00"], "market_scope": "asia"},
        )
        self.assertEqual(["PMAAV00"], started["parameters"]["symbols"])
        current = self.wait_for_terminal(jobs, started["id"])
        self.assertEqual("succeeded", current["state"])
        self.assertEqual({"symbols": ["PMAAV00"], "market_scope": "asia"}, received)


if __name__ == "__main__":
    unittest.main()
