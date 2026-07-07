import unittest

from opencode_session.remote_journal import (
    OUTBOX_STATE_APPLIED,
    OUTBOX_STATE_INTENT,
    OUTBOX_STATE_REMOTE_SUCCEEDED,
    OUTBOX_STATE_UNCERTAIN,
    RemoteJournalRecord,
    RemoteMutationJournal,
    RemoteMutationOperation,
    RemoteMutationRecovery,
)

try:
    from tests.remote_journal_helpers import applied_record, intent_record
except ModuleNotFoundError:
    from remote_journal_helpers import applied_record, intent_record


class RemoteJournalRecordTest(unittest.TestCase):
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

    def test_record_get_rejects_untyped_field_names(self):
        record = RemoteJournalRecord("mutation-1", "prompt", {"message_id": "msg_1"})

        with self.assertRaisesRegex(TypeError, "record field name"):
            record.get(None)

    def test_journal_rejects_untyped_boundary_names(self):
        with self.assertRaisesRegex(TypeError, "journal field"):
            RemoteMutationJournal(None)

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
                {"id": "remote-succeeded", "kind": "prompt", "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED},
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
            ("intent", "remote-succeeded", "applied", "uncertain", "legacy-created", "legacy-pending"),
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
        self.assertEqual(
            tuple(
                entry["id"]
                for entry in journal.pending_entries(
                    run,
                    kind="prompt",
                    outbox_states=(OUTBOX_STATE_REMOTE_SUCCEEDED,),
                )
            ),
            ("remote-succeeded",),
        )

    def test_mark_applied_rejects_record_id_mismatch(self):
        journal = RemoteMutationJournal("journal")

        with self.assertRaisesRegex(ValueError, "mutation-1"):
            journal.mark_applied(
                {"journal": [{"id": "mutation-1", "kind": "prompt"}]},
                "mutation-1",
                applied_record("mutation-2"),
            )


class RemoteJournalRecoveryTest(unittest.TestCase):
    def test_recovery_values_by_owner_ignores_untyped_owners(self):
        recovery = RemoteMutationRecovery("journal")
        run = {
            "journal": [
                {
                    "id": "mutation-1",
                    "kind": "prompt",
                    "worker_id": None,
                    "message_id": "msg_skipped",
                },
                {
                    "id": "mutation-2",
                    "kind": "prompt",
                    "worker_id": "planner",
                    "prompt_ids": ["msg_1", None, "msg_2"],
                    "message_id": "msg_2",
                },
            ]
        }

        self.assertEqual(
            recovery.values_by_owner(
                run,
                kind="prompt",
                owner_field="worker_id",
                value_fields=("message_id",),
                list_fields=("prompt_ids",),
            ),
            {"planner": ["msg_1", "msg_2"]},
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
