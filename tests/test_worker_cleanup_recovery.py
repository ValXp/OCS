import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.remote_journal import OUTBOX_STATE_APPLIED, OUTBOX_STATE_INTENT, OUTBOX_STATE_REMOTE_SUCCEEDED
from opencode_session.worker_cleanup_recovery import (
    cleanup_created_worker_sessions,
    recoverable_created_worker_sessions_by_worker,
)
from opencode_session.worker_session_provisioning import WORKER_SESSION_JOURNAL_FIELD
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_state import WorkerRecord, worker_field

try:
    from tests.worker_execution_helpers import FakeClient
except ModuleNotFoundError:
    from worker_execution_helpers import FakeClient


class WorkerCleanupRecoveryTest(unittest.TestCase):
    def test_cleanup_created_worker_sessions_clears_stale_sessions_after_single_session_success(self):
        worker = WorkerRecord(
            "worker",
            {
                "id": "worker",
                "cleanup": {
                    "requested": True,
                    "deleted": False,
                    "error": "DELETE /api/session/ses_old failed: HTTP 500",
                    "sessions": ["ses_old", "ses_retry"],
                },
            },
        ).to_worker()
        client = FakeClient([])

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_new"])

        self.assertEqual(client.requests, [("delete", "ses_new"), ("get", "ses_new")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_new"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": True})

    def test_cleanup_created_worker_sessions_treats_missing_session_as_deleted(self):
        worker = WorkerRecord.default_fields("worker")
        client = FakeClient(
            [],
            delete_failures={"ses_missing": OpenCodeApiError("session not found", status=404)},
        )

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_missing"])

        self.assertEqual(client.requests, [("delete", "ses_missing"), ("get", "ses_missing")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_missing"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": True})

    def test_cleanup_created_worker_sessions_persists_only_pending_sessions_after_partial_failure(self):
        worker = WorkerRecord.default_fields("worker")
        failure = OpenCodeApiError("DELETE /api/session/ses_live failed: HTTP 500", status=500)
        client = FakeClient([], delete_failures={"ses_live": failure})

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_deleted", "ses_live"])

        self.assertEqual(
            client.requests,
            [("delete", "ses_deleted"), ("get", "ses_deleted"), ("delete", "ses_live")],
        )
        self.assertEqual(outcome.deleted_session_ids, ["ses_deleted"])
        self.assertIs(outcome.error, failure)
        self.assertEqual(
            worker_field(worker, "cleanup"),
            {
                "requested": True,
                "deleted": False,
                "error": "DELETE /api/session/ses_live failed: HTTP 500",
                "sessions": ["ses_live"],
                "verified": ["ses_deleted"],
            },
        )
        self.assertEqual(
            recoverable_created_worker_sessions_by_worker({"workers": {"worker": worker}}),
            {"worker": ["ses_live"]},
        )

    def test_recoverable_created_worker_sessions_merges_cleanup_and_journal_transactions(self):
        run = {
            "workers": {
                "worker": hydrate_worker_record(
                    {
                        "id": "worker",
                        "cleanup": {
                            "deleted": False,
                            "sessions": ["ses_worker_cleanup", "ses_duplicate"],
                        },
                    },
                    "worker",
                ),
                "deleted": hydrate_worker_record(
                    {
                        "id": "deleted",
                        "cleanup": {
                            "deleted": True,
                            "sessions": ["ses_deleted"],
                        },
                    },
                    "deleted",
                ),
            },
            WORKER_SESSION_JOURNAL_FIELD: [
                {
                    "id": "worker-session-intent-1",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "worker",
                    "cleanup_requested": True,
                    "created_session_ids": ["ses_duplicate", "ses_created"],
                    "session_id": "ses_created",
                },
                {
                    "id": "worker-session-intent-2",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "other",
                    "cleanup_requested": True,
                    "session_id": "ses_other",
                },
                {
                    "id": "worker-session-intent-3",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "skipped",
                    "cleanup_requested": False,
                    "session_id": "ses_skipped",
                },
                {
                    "id": "worker-session-intent-4",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
                    "worker_id": "remote-succeeded",
                    "cleanup_requested": True,
                    "session_id": "ses_remote_succeeded",
                },
                {
                    "id": "worker-session-intent-5",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "worker_id": "pending",
                    "cleanup_requested": True,
                    "session_id": "ses_pending",
                },
            ],
        }

        self.assertEqual(
            recoverable_created_worker_sessions_by_worker(run),
            {
                "worker": ["ses_worker_cleanup", "ses_duplicate", "ses_created"],
                "other": ["ses_other"],
                "remote-succeeded": ["ses_remote_succeeded"],
            },
        )


if __name__ == "__main__":
    unittest.main()
