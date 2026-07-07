import unittest

from opencode_session.remote_journal import (
    MISSING_INTENT_RECORD_APPLIED,
    OUTBOX_STATE_APPLIED,
    OUTBOX_STATE_INTENT,
    OUTBOX_STATE_UNCERTAIN,
    PersistedRemoteMutationJournal,
    RecordedIntent,
    RemoteJournalRecord,
    RemoteMutationJournal,
    RemoteMutationOperation,
    RemoteMutationRecovery,
    RemoteMutationResult,
)
from opencode_session.run_store import RunStoreError


PROMPT_OPERATION = RemoteMutationOperation(
    kind="prompt",
    discard_cleanup_operation="discard_prompt",
    finalize_cleanup_operation="finalize_prompt",
    call_remote_operation="call_prompt",
)


def intent_record(record_id="mutation-1", kind="prompt", session_id="ses_1", *, message_id=None):
    fields = {"session_id": session_id}
    if message_id is not None:
        fields["message_id"] = message_id
    return RemoteJournalRecord(record_id, kind, fields)


def applied_record(record_id="mutation-1", kind="prompt", status="applied", *, message_id=None):
    fields = {"status": status}
    if message_id is not None:
        fields["message_id"] = message_id
    return RemoteJournalRecord(record_id, kind, fields)


def remember_message(message_id):
    def mutate(run):
        run["applied_message_id"] = message_id

    return mutate


class RemoteMutationJournalTest(unittest.TestCase):
    def test_operation_rejects_invalid_identifiers(self):
        with self.assertRaisesRegex(TypeError, "non-empty string"):
            RemoteMutationOperation(
                kind="",
                discard_cleanup_operation="discard_prompt",
                finalize_cleanup_operation="finalize_prompt",
                call_remote_operation="call_prompt",
            )

        with self.assertRaisesRegex(TypeError, "non-empty string"):
            RemoteMutationOperation(
                kind="prompt",
                discard_cleanup_operation=None,
                finalize_cleanup_operation="finalize_prompt",
                call_remote_operation="call_prompt",
            )

    def test_record_rejects_reserved_fields(self):
        with self.assertRaisesRegex(ValueError, "id"):
            RemoteJournalRecord("mutation-1", "prompt", {"id": "other"})

        with self.assertRaisesRegex(ValueError, "kind"):
            RemoteJournalRecord("mutation-1", "prompt", {"kind": "other"})

        with self.assertRaisesRegex(ValueError, "outbox_state"):
            RemoteJournalRecord("mutation-1", "prompt", {"outbox_state": OUTBOX_STATE_INTENT})

    def test_journal_rejects_untyped_record_writes(self):
        journal = RemoteMutationJournal("journal")

        with self.assertRaisesRegex(TypeError, "RemoteJournalRecord"):
            journal.record_intent({}, {"id": "mutation-1", "kind": "prompt"})

        with self.assertRaisesRegex(TypeError, "RemoteJournalRecord"):
            journal.mark_applied(
                {"journal": [{"id": "mutation-1", "kind": "prompt"}]},
                "mutation-1",
                {"id": "mutation-1", "kind": "prompt", "status": "applied"},
            )

    def test_records_marks_applied_finalizes_and_filters_pending_entries(self):
        run = {"journal": "corrupt"}
        journal = RemoteMutationJournal("journal")

        journal.record_intent(run, intent_record(message_id="msg_1"))
        journal.record_intent(run, intent_record("mutation-2", "abort"))
        journal.mark_applied(run, "mutation-1", applied_record())

        self.assertEqual(
            journal.pending_entries(run, kind="prompt"),
            (
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "session_id": "ses_1",
                    "message_id": "msg_1",
                    "status": "applied",
                },
            ),
        )

        journal.finalize(run, "mutation-1")
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-2",
                    "kind": "abort",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "session_id": "ses_1",
                }
            ],
        )
        journal.finalize(run, "mutation-2")
        self.assertNotIn("journal", run)

    def test_pending_entries_are_state_aware_and_preserve_legacy_pending_entries(self):
        run = {
            "journal": [
                {"id": "intent", "kind": "prompt", "outbox_state": OUTBOX_STATE_INTENT},
                {"id": "applied", "kind": "prompt", "outbox_state": OUTBOX_STATE_APPLIED},
                {"id": "uncertain", "kind": "prompt", "outbox_state": OUTBOX_STATE_UNCERTAIN},
                {"id": "legacy-created", "kind": "prompt", "status": "created"},
                {"id": "legacy-pending", "kind": "prompt"},
                {"id": "finalized", "kind": "prompt", "outbox_state": "finalized"},
                {"id": "other", "kind": "abort", "outbox_state": OUTBOX_STATE_APPLIED},
            ]
        }
        journal = RemoteMutationJournal("journal")

        self.assertEqual(
            tuple(entry["id"] for entry in journal.pending_entries(run, kind="prompt")),
            ("intent", "applied", "uncertain", "legacy-created", "legacy-pending"),
        )
        self.assertEqual(
            tuple(
                entry["id"]
                for entry in journal.pending_entries(
                    run,
                    kind="prompt",
                    outbox_states=(OUTBOX_STATE_APPLIED,),
                )
            ),
            ("applied", "legacy-created"),
        )

    def test_mark_applied_rejects_record_id_mismatch(self):
        journal = RemoteMutationJournal("journal")

        with self.assertRaisesRegex(ValueError, "mutation-1"):
            journal.mark_applied(
                {"journal": [{"id": "mutation-1", "kind": "prompt"}]},
                "mutation-1",
                applied_record("mutation-2"),
            )

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
                    "outbox_state": OUTBOX_STATE_APPLIED,
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

    def test_runner_records_intent_calls_remote_applies_and_finalizes(self):
        run = {"name": "demo", "workers": {"worker": {"session_id": "ses_latest"}}}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )
        transaction = journal.transaction("mutation-1", PROMPT_OPERATION)

        def intent_factory(latest_run):
            session_id = latest_run["workers"]["worker"]["session_id"]
            calls.append(("intent", session_id))
            return intent_record(session_id=session_id, message_id="msg_1")

        def call_remote(latest_run, intent):
            calls.append(("remote", intent.fields["session_id"], latest_run["journal"][0]["message_id"]))
            return {"message_id": "msg_1"}

        def apply_result(remote_result, intent):
            return RemoteMutationResult.finalize(run_update=remember_message(remote_result["message_id"]))

        execution = transaction.runner().execute(
            run,
            intent_factory=intent_factory,
            call_remote=call_remote,
            apply_result=apply_result,
        )

        self.assertIs(execution.run, run)
        self.assertEqual(execution.remote_result, {"message_id": "msg_1"})
        self.assertEqual(execution.intent.fields["session_id"], "ses_latest")
        self.assertEqual(run["applied_message_id"], "msg_1")
        self.assertNotIn("journal", run)
        self.assertEqual(calls, ["persist", ("intent", "ses_latest"), ("remote", "ses_latest", "msg_1"), "persist"])

    def test_runner_can_leave_intent_pending_after_remote_result(self):
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

        execution = transaction.runner().execute(
            run,
            intent_factory=lambda latest_run: intent_record(),
            call_remote=lambda latest_run, intent: {"created_session_id": None},
            apply_result=lambda remote_result, intent: RemoteMutationResult.keep_pending(),
        )

        self.assertIs(execution.run, run)
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "session_id": "ses_1",
                }
            ],
        )

    def test_runner_records_remote_applied_state_without_finalizing(self):
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

        execution = transaction.runner().execute(
            run,
            intent_factory=lambda latest_run: intent_record(),
            call_remote=lambda latest_run, intent: {"message_id": "msg_1"},
            apply_result=lambda remote_result, intent: RemoteMutationResult.record_applied(
                applied_record(message_id=remote_result["message_id"]),
                run_update=remember_message(remote_result["message_id"]),
                missing_intent_policy=MISSING_INTENT_RECORD_APPLIED,
            ),
        )

        self.assertIs(execution.run, run)
        self.assertEqual(run["applied_message_id"], "msg_1")
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "session_id": "ses_1",
                    "message_id": "msg_1",
                    "status": "applied",
                }
            ],
        )

    def test_runner_marks_intent_uncertain_after_remote_failure(self):
        run = {"name": "demo"}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )
        transaction = journal.transaction("mutation-1", PROMPT_OPERATION)

        def reject_remote(latest_run, intent):
            raise RuntimeError("remote rejected")

        with self.assertRaisesRegex(RuntimeError, "remote rejected"):
            transaction.runner().execute(
                run,
                intent_factory=lambda latest_run: intent_record(),
                call_remote=reject_remote,
            )

        self.assertEqual(calls, ["persist", "persist"])
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_UNCERTAIN,
                    "session_id": "ses_1",
                    "status": "uncertain",
                    "uncertain_failure": {
                        "operation": "call_prompt",
                        "error_type": "RuntimeError",
                        "message": "remote rejected",
                        "recorded_at": "2026-07-05T00:00:00Z",
                    },
                }
            ],
        )

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

    def test_runner_keeps_journal_recoverable_after_local_apply_or_finalize_failure(self):
        run = {"name": "demo"}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 2:
                raise RunStoreError("forced finalize failure")
            mutator(run)
            return run

        journal = PersistedRemoteMutationJournal(
            "journal",
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
        )
        transaction = journal.transaction("mutation-1", PROMPT_OPERATION)

        def apply_result(remote_result, intent):
            return RemoteMutationResult.finalize(run_update=remember_message(remote_result["message_id"]))

        with self.assertRaisesRegex(RunStoreError, "forced finalize failure"):
            transaction.runner().execute(
                run,
                intent_factory=lambda latest_run: intent_record(),
                call_remote=lambda latest_run, intent: {"message_id": "msg_1"},
                apply_result=apply_result,
            )

        self.assertEqual(calls, ["persist", "persist"])
        self.assertNotIn("applied_message_id", run)
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "session_id": "ses_1",
                }
            ],
        )

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
