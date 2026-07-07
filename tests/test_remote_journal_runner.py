import unittest

from opencode_session.remote_journal import (
    OUTBOX_STATE_INTENT,
    OUTBOX_STATE_REMOTE_SUCCEEDED,
    OUTBOX_STATE_UNCERTAIN,
    MISSING_INTENT_RECORD_APPLIED,
    PersistedRemoteMutationJournal,
    RemoteMutationResult,
)
from opencode_session.run_store import RunStoreError

try:
    from tests.remote_journal_helpers import PROMPT_OPERATION, applied_record, intent_record, remember_message
except ModuleNotFoundError:
    from remote_journal_helpers import PROMPT_OPERATION, applied_record, intent_record, remember_message


class RemoteJournalRunnerTest(unittest.TestCase):
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
        self.assertEqual(
            calls,
            ["persist", ("intent", "ses_latest"), ("remote", "ses_latest", "msg_1"), "persist", "persist"],
        )

    def test_runner_can_leave_remote_success_pending_after_remote_result(self):
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
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
                    "session_id": "ses_1",
                }
            ],
        )

    def test_runner_records_remote_success_before_local_apply_result_failure(self):
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

        def call_remote(latest_run, intent):
            calls.append(("remote", latest_run["journal"][0]["outbox_state"]))
            return {"message_id": "msg_1"}

        def apply_result(remote_result, intent):
            calls.append(("apply", run["journal"][0]["outbox_state"]))
            raise RuntimeError("local result failed")

        with self.assertRaisesRegex(RuntimeError, "local result failed"):
            transaction.runner().execute(
                run,
                intent_factory=lambda latest_run: intent_record(),
                call_remote=call_remote,
                apply_result=apply_result,
            )

        self.assertEqual(
            calls,
            ["persist", ("remote", OUTBOX_STATE_INTENT), "persist", ("apply", OUTBOX_STATE_REMOTE_SUCCEEDED)],
        )
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
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
                    "outbox_state": "applied",
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

    def test_runner_keeps_journal_recoverable_after_local_apply_or_finalize_failure(self):
        run = {"name": "demo"}
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 3:
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

        self.assertEqual(calls, ["persist", "persist", "persist"])
        self.assertNotIn("applied_message_id", run)
        self.assertEqual(
            run["journal"],
            [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
                    "session_id": "ses_1",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
