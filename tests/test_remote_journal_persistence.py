import unittest

from opencode_session.remote_journal import PersistedRemoteMutationJournal, RecordedIntent
from opencode_session.run_store import RunStoreError

try:
    from tests.remote_journal_helpers import PROMPT_OPERATION, applied_record, intent_record
except ModuleNotFoundError:
    from remote_journal_helpers import PROMPT_OPERATION, applied_record, intent_record


class PersistedRemoteJournalTest(unittest.TestCase):
    def test_persisted_best_effort_cleanup_failure_marks_pending_entry(self):
        run = {"name": "demo", "journal": [{"id": "mutation-1", "kind": "prompt"}]}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 1:
                raise RunStoreError("forced cleanup failure")
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
                "error_type": "RunStoreError",
                "message": "forced cleanup failure",
                "recorded_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_persisted_best_effort_cleanup_failure_propagates_non_persistence_error(self):
        run = {"name": "demo", "journal": [{"id": "mutation-1", "kind": "prompt"}]}

        def persist_run_mutation(run, mutator):
            raise ValueError("not a persistence failure")

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )

        with self.assertRaisesRegex(ValueError, "not a persistence failure"):
            journal.discard_intent_best_effort(
                run,
                "mutation-1",
                operation="discard_remote_mutation",
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

        recorded = journal.record_intent_from(
            run,
            lambda latest_run: intent_record(
                session_id=latest_run["workers"]["worker"]["session_id"],
            ),
        )

        self.assertIsInstance(recorded, RecordedIntent)
        self.assertIs(recorded.run, run)
        self.assertEqual(recorded.intent.fields["session_id"], "ses_latest")
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
        transaction = journal.transaction("mutation-1", PROMPT_OPERATION)

        recorded = transaction.record_intent_from(
            run,
            lambda latest_run: intent_record(
                session_id=latest_run["workers"]["worker"]["session_id"],
                message_id="msg_1",
            ),
        )
        run = recorded.run
        self.assertEqual(recorded.intent.fields["message_id"], "msg_1")
        run = transaction.mark_applied(run, applied_record())
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": "applied",
                    "session_id": "ses_latest",
                    "message_id": "msg_1",
                    "status": "applied",
                }
            ],
        )

        run = transaction.finalize(run)

        self.assertNotIn("journal", run)

    def test_transaction_rejects_records_for_other_operations(self):
        run = {"name": "demo"}

        def persist_run_mutation(run, mutator):
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )
        transaction = journal.transaction("mutation-1", PROMPT_OPERATION)

        with self.assertRaisesRegex(ValueError, "kind"):
            transaction.record_intent(run, intent_record(kind="abort"))

        with self.assertRaisesRegex(ValueError, "id"):
            transaction.record_intent(run, intent_record("mutation-2"))

    def test_mark_uncertain_best_effort_returns_original_run_after_persistence_failure(self):
        run = {"name": "demo", "journal": [{"id": "mutation-1", "kind": "prompt"}]}

        def persist_run_mutation(run, mutator):
            raise RunStoreError("forced uncertain persistence failure")

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )

        updated_run = journal.mark_uncertain_best_effort(
            run,
            "mutation-1",
            RuntimeError("remote rejected"),
            operation="call_prompt",
        )

        self.assertIs(updated_run, run)
        self.assertEqual(run["journal"], [{"id": "mutation-1", "kind": "prompt"}])

    def test_mark_uncertain_best_effort_propagates_non_persistence_error(self):
        run = {"name": "demo", "journal": [{"id": "mutation-1", "kind": "prompt"}]}

        def persist_run_mutation(run, mutator):
            raise ValueError("not a persistence failure")

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )

        with self.assertRaisesRegex(ValueError, "not a persistence failure"):
            journal.mark_uncertain_best_effort(
                run,
                "mutation-1",
                RuntimeError("remote rejected"),
                operation="call_prompt",
            )


if __name__ == "__main__":
    unittest.main()
