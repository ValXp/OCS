import unittest

from opencode_session.remote_journal import (
    PersistedRemoteMutationJournal,
    RemoteMutationJournal,
    RemoteMutationRecovery,
)


class RemoteMutationJournalTest(unittest.TestCase):
    def test_records_marks_applied_finalizes_and_filters_pending_entries(self):
        run = {"journal": "corrupt"}
        journal = RemoteMutationJournal("journal")

        journal.record_intent(run, {"id": "mutation-1", "kind": "prompt", "message_id": "msg_1"})
        journal.record_intent(run, {"id": "mutation-2", "kind": "abort", "session_id": "ses_1"})
        journal.mark_applied(run, "mutation-1", {"status": "applied"})

        self.assertEqual(
            journal.pending_entries(run, kind="prompt"),
            ({"id": "mutation-1", "kind": "prompt", "message_id": "msg_1", "status": "applied"},),
        )

        journal.finalize(run, "mutation-1")
        self.assertEqual(run["journal"], [{"id": "mutation-2", "kind": "abort", "session_id": "ses_1"}])
        journal.finalize(run, "mutation-2")
        self.assertNotIn("journal", run)

    def test_persisted_best_effort_cleanup_failure_marks_pending_entry(self):
        run = {"name": "demo", "journal": [{"id": "mutation-1", "kind": "prompt"}]}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 1:
                raise RuntimeError("forced cleanup failure")
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )

        updated_run = journal.discard_intent_best_effort(
            run,
            "mutation-1",
            operation="discard_remote_mutation",
        )

        self.assertIs(updated_run, run)
        self.assertEqual(calls, ["persist", "persist"])
        self.assertEqual(
            run["journal"][0]["cleanup_failure"],
            {
                "operation": "discard_remote_mutation",
                "error_type": "RuntimeError",
                "message": "forced cleanup failure",
                "recorded_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_record_intent_from_builds_entry_against_latest_run(self):
        run = {"name": "demo", "workers": {"worker": {"session_id": "ses_old"}}}

        def persist_run_mutation(run, mutator):
            run["workers"]["worker"]["session_id"] = "ses_latest"
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )

        journal.record_intent_from(
            run,
            lambda latest_run: {
                "id": "mutation-1",
                "kind": "prompt",
                "session_id": latest_run["workers"]["worker"]["session_id"],
            },
        )

        self.assertEqual(run["journal"][0]["session_id"], "ses_latest")

    def test_transaction_records_identity_and_lifecycle(self):
        run = {"name": "demo", "workers": {"worker": {"session_id": "ses_old"}}}

        def persist_run_mutation(run, mutator):
            run["workers"]["worker"]["session_id"] = "ses_latest"
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )
        transaction = journal.transaction(
            "mutation-1",
            "prompt",
            discard_operation="discard_remote_mutation",
            finalize_operation="finalize_remote_mutation",
        )

        run = transaction.record_intent_from(
            run,
            lambda latest_run: {
                "session_id": latest_run["workers"]["worker"]["session_id"],
                "message_id": "msg_1",
            },
        )
        run = transaction.mark_applied(run, {"status": "applied"})
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "session_id": "ses_latest",
                    "message_id": "msg_1",
                    "status": "applied",
                }
            ],
        )

        run = transaction.finalize(run)

        self.assertNotIn("journal", run)

    def test_recovery_collects_unique_values_by_owner_from_pending_transactions(self):
        run = {
            "journal": [
                {
                    "id": "mutation-1",
                    "kind": "worker_session_create",
                    "worker_id": "worker",
                    "cleanup_requested": True,
                    "created_session_ids": ["ses_1", "", "ses_2"],
                    "session_id": "ses_1",
                },
                {
                    "id": "mutation-2",
                    "kind": "worker_session_create",
                    "worker_id": "worker",
                    "cleanup_requested": False,
                    "session_id": "ses_skipped",
                },
                {
                    "id": "mutation-3",
                    "kind": "steer_prompt",
                    "worker_id": "worker",
                    "session_id": "ses_other_kind",
                },
            ]
        }
        recovery = RemoteMutationRecovery("journal")

        self.assertEqual(
            recovery.values_by_owner(
                run,
                kind="worker_session_create",
                owner_field="worker_id",
                list_fields=("created_session_ids",),
                value_fields=("session_id",),
                required_fields={"cleanup_requested": True},
            ),
            {"worker": ["ses_1", "ses_2"]},
        )


if __name__ == "__main__":
    unittest.main()
