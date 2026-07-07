import unittest

from opencode_session.worker_active_attempt_recovery import recover_expired_active_attempts
from opencode_session.worker_state import WorkerRecord


NOW = "2026-07-03T00:00:00Z"
LATER = "2026-07-03T00:00:01Z"


class WorkerActiveAttemptRecoveryTest(unittest.TestCase):
    def test_invalid_runtime_timeout_seconds_does_not_become_no_timeout(self):
        worker = WorkerRecord.default_fields("worker")
        worker.update_canonical_fields(
            lifecycle_state="active_wait",
            timeout_seconds=0.05,
            timeout_started_at=NOW,
        )
        worker.append_attempt(
            {
                "id": "attempt-1",
                "status": "active",
                "started_at": NOW,
                "finished_at": None,
            }
        )
        worker.timeout_seconds = "not-a-number"

        with self.assertRaisesRegex(TypeError, "timeout_seconds"):
            recover_expired_active_attempts({"worker": worker}, now=lambda: LATER)


if __name__ == "__main__":
    unittest.main()
