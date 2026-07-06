import unittest

from opencode_session.remote_journal import PersistedRemoteMutationJournal, RemoteMutationJournal


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


if __name__ == "__main__":
    unittest.main()
