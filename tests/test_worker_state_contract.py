import unittest

from opencode_session.worker_storage_adapter import (
    hydrate_worker_record,
    migrate_persisted_worker_snapshot,
    normalize_worker_snapshot_for_storage,
)
from opencode_session.worker_snapshot_transition import worker_snapshot_transition_patch
from opencode_session.run_record import upsert_worker_record
from opencode_session.schema_worker import WorkerRequiredFields, WorkerSnapshotRecord
from opencode_session.worker_state import WORKER_RECORD_CANONICAL_FIELD_NAMES, WorkerRecord


class WorkerStateContractTest(unittest.TestCase):
    def test_worker_record_projects_public_output_from_domain_state(self):
        cases = (
            ("queued", "queued", "start"),
            ("active_wait", "active", "wait"),
            ("active_retry", "active", "retry"),
            ("blocked_dependency", "blocked", "resolve_blocker"),
            ("done_collect", "done", "collect"),
            ("failed_retry", "failed", "retry"),
            ("failed_terminal", "failed", "none"),
            ("timeout_failed_retry", "failed", "retry"),
            ("timeout_terminal", "timeout", "none"),
            ("timeout_aborted", "aborted", "none"),
            ("aborted", "aborted", "none"),
        )

        for lifecycle_state, status, action in cases:
            with self.subTest(lifecycle_state=lifecycle_state):
                worker = WorkerRecord.from_worker(
                    {"id": lifecycle_state, "prompt": "Work", "lifecycle_state": lifecycle_state},
                    lifecycle_state,
                ).to_worker()
                output = worker.to_output_dict()

                self.assertEqual(output["status"], status)
                self.assertEqual(output["next_eligible_action"], action)
                self.assertEqual(worker.lifecycle_state, lifecycle_state)
                self.assertNotIn("status", worker.to_snapshot())
                self.assertNotIn("next_eligible_action", worker.to_snapshot())

    def test_worker_record_explicit_api_keeps_snapshot_canonical(self):
        worker = WorkerRecord.default_fields("review")

        worker.update_canonical_fields(
            role="reviewer",
            prompt="Review the change",
            lifecycle_state="active_wait",
        )
        worker.remember_prompt_id("msg_review")

        snapshot = worker.to_snapshot()
        output = worker.to_output_dict()

        self.assertEqual(worker.worker_id, "review")
        self.assertEqual(worker.role, "reviewer")
        self.assertEqual(worker.prompt, "Review the change")
        self.assertTrue(worker.has_prompt)
        self.assertEqual(worker.prompt_ids, ["msg_review"])
        self.assertEqual(output["status"], "active")
        self.assertEqual(output["next_eligible_action"], "wait")
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertEqual(snapshot["prompt_ids"], ["msg_review"])
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_worker_snapshot_schema_declares_runtime_persisted_fields(self):
        self.assertEqual(
            set(WorkerSnapshotRecord.__annotations__),
            WORKER_RECORD_CANONICAL_FIELD_NAMES,
        )

    def test_worker_default_storage_and_output_follow_required_schema_contract(self):
        expected_defaults = {
            "id": "review",
            "role": None,
            "session_id": None,
            "agent": None,
            "model": None,
            "dependencies": [],
            "prompt_ids": [],
            "retry_count": 0,
            "retry_limit": 0,
            "retryable_failures": [],
            "timeout_seconds": None,
            "timeout_policy": "timeout",
            "timeout_started_at": None,
            "timed_out_at": None,
            "lifecycle_state": "queued",
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": None,
            "last_failure_reason": None,
            "blockers": [],
            "output_refs": [],
        }
        defaults = WorkerRecord.default_snapshot_fields("review")
        worker = WorkerRecord.default_fields("review")
        stored = normalize_worker_snapshot_for_storage({"id": "review"}, "review")
        output = worker.to_output_dict()

        self.assertEqual(defaults, expected_defaults)
        self.assertEqual(stored, expected_defaults)
        self.assertEqual(set(defaults), set(WorkerRequiredFields.__annotations__))
        for field_name, expected_value in expected_defaults.items():
            with self.subTest(field_name=field_name):
                self.assertEqual(output[field_name], expected_value)
        self.assertEqual(output["status"], "queued")
        self.assertEqual(output["next_eligible_action"], "start")

        defaults["dependencies"].append("mutated")
        self.assertEqual(WorkerRecord.default_snapshot_fields("review")["dependencies"], [])

    def test_storage_migration_coerces_persisted_runtime_fields(self):
        legacy_worker = {
            "id": "review",
            "lifecycle_state": "active_wait",
            "dependencies": "not-a-list",
            "prompt_ids": "not-a-list",
            "retry_count": "2",
            "retry_limit": "3",
            "retryable_failures": "not-a-list",
            "timeout_policy": "failed",
            "blockers": "not-a-list",
            "output_refs": "not-a-list",
            "attempts": "not-a-list",
            "unknown_plugin_state": {"attempt": 2},
        }

        migrated = migrate_persisted_worker_snapshot(legacy_worker, "review")

        self.assertEqual(migrated["retry_count"], 2)
        self.assertEqual(migrated["retry_limit"], 3)
        for field_name in (
            "dependencies",
            "prompt_ids",
            "retryable_failures",
            "blockers",
            "output_refs",
            "attempts",
        ):
            with self.subTest(field_name=field_name):
                self.assertEqual(migrated[field_name], [])
        self.assertEqual(migrated["timeout_policy"], "failed")
        self.assertEqual(migrated["unknown_plugin_state"], {"attempt": 2})

        invalid_timeout_policy = migrate_persisted_worker_snapshot(
            {"id": "review", "timeout_policy": "unknown-policy"},
            "review",
        )
        self.assertEqual(invalid_timeout_policy["timeout_policy"], "timeout")

    def test_snapshot_replay_patch_projects_storage_replay_behavior(self):
        worker = WorkerRecord.default_fields("review")
        worker.update_canonical_fields(
            session_id="ses_review",
            lifecycle_state="failed_terminal",
            retry_count=1,
            timeout_started_at="2026-07-06T00:00:00Z",
            timed_out_at="2026-07-06T00:01:00Z",
            failure_category="provider",
            failure_reason="provider failed",
            last_failure_category="provider",
            last_failure_reason="provider failed",
            blockers=["provider"],
            output_refs=["assistant:msg_review"],
            error="provider failed",
            failure_retryable=False,
            manual_retry_required=True,
            cleanup={"requested": True, "deleted": False},
            abort={"accepted": False},
            attempts=[{"id": "attempt-1"}],
            result={"status": "failed"},
        )
        worker.remember_prompt_id("prompt-review")

        patch = worker_snapshot_transition_patch(worker)
        expected_replayed_fields = {
            "id": "review",
            "retry_count": 1,
            "timeout_started_at": "2026-07-06T00:00:00Z",
            "timed_out_at": "2026-07-06T00:01:00Z",
            "lifecycle_state": "failed_terminal",
            "failure_category": "provider",
            "failure_reason": "provider failed",
            "last_failure_category": "provider",
            "last_failure_reason": "provider failed",
            "blockers": ["provider"],
            "output_refs": ["assistant:msg_review"],
            "error": "provider failed",
            "failure_retryable": False,
            "manual_retry_required": True,
            "cleanup": {"requested": True, "deleted": False},
            "abort": {"accepted": False},
            "attempts": [{"id": "attempt-1"}],
            "result": {"status": "failed"},
        }

        self.assertEqual(patch.fields, expected_replayed_fields)
        self.assertEqual(patch.set_if_missing_fields, {"session_id": "ses_review"})
        self.assertEqual(patch.prompt_ids, ("prompt-review",))
        self.assertEqual(patch.accepted_abort_prompt_ids, ("prompt-review",))
        self.assertEqual(patch.accepted_abort_fields, {"cleanup": {"requested": True, "deleted": False}})

    def test_snapshot_replay_absent_removals_clear_stale_transient_failure_fields(self):
        patch = worker_snapshot_transition_patch({"id": "review"}, "review")

        self.assertEqual(set(patch.remove_fields), {"error", "failure_retryable", "manual_retry_required"})
        self.assertEqual(patch.accepted_abort_fields, {})

    def test_run_worker_upsert_persists_supported_worker_changes(self):
        upsert_values = {
            "role": "reviewer",
            "session_id": "ses_review",
            "agent": "review-agent",
            "model": "review-model",
            "dependencies": ["build"],
            "prompt_ids": ["prompt-review"],
            "retry_count": 1,
            "retry_limit": 2,
            "retryable_failures": ["provider"],
            "timeout_seconds": 30.0,
            "timeout_policy": "failed",
            "lifecycle_state": "active_wait",
            "blockers": ["dependency"],
            "output_refs": ["assistant:msg_review"],
            "prompt": "Review the change",
        }
        run = {"workers": {}}

        upsert_worker_record(
            run,
            "review",
            upsert_values,
            now="2026-07-06T00:00:00Z",
        )

        worker = run["workers"]["review"]
        for field_name, expected_value in upsert_values.items():
            with self.subTest(field_name=field_name):
                self.assertEqual(getattr(worker, field_name), expected_value)
        self.assertEqual(run["updated_at"], "2026-07-06T00:00:00Z")

    def test_hydration_boundary_normalizes_legacy_public_state_for_output(self):
        cases = (
            (
                "done worker is collectable",
                {"id": "done", "prompt": "Done", "status": "done"},
                "done",
                "collect",
            ),
            (
                "active retry worker is retryable",
                {"id": "active_retry", "prompt": "Retry", "status": "active", "next_eligible_action": "retry"},
                "active",
                "retry",
            ),
            (
                "retryable failure remains executable",
                {
                    "id": "failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                "failed",
                "retry",
            ),
            (
                "terminal failure is not retryable",
                {"id": "failed_terminal", "prompt": "Investigate", "status": "failed"},
                "failed",
                "none",
            ),
            (
                "timeout blocker keeps resolution action",
                {
                    "id": "blocked_timeout",
                    "prompt": "Unblock",
                    "status": "blocked",
                    "blockers": ["timeout"],
                },
                "blocked",
                "resolve_blocker",
            ),
            (
                "canonical lifecycle wins over stale public fields",
                {
                    "id": "active_wait",
                    "prompt": "Wait",
                    "lifecycle_state": "active_wait",
                    "status": "done",
                    "next_eligible_action": "collect",
                },
                "active",
                "wait",
            ),
        )

        for name, persisted_worker, expected_status, expected_action in cases:
            with self.subTest(name=name):
                worker = hydrate_worker_record(persisted_worker, persisted_worker["id"])
                snapshot = normalize_worker_snapshot_for_storage(persisted_worker, persisted_worker["id"])
                output = worker.to_output_dict()

                self.assertIsInstance(worker, WorkerRecord)
                self.assertEqual(output["status"], expected_status)
                self.assertEqual(output["next_eligible_action"], expected_action)
                self.assertNotIn("status", snapshot)
                self.assertNotIn("next_eligible_action", snapshot)

    def test_storage_adapter_projects_retry_timeout_edge_cases_as_public_output(self):
        cases = (
            (
                "failed retry budget",
                {
                    "id": "failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                "failed",
                "retry",
            ),
            (
                "failed retry disabled",
                {
                    "id": "failed_terminal",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                    "failure_retryable": False,
                },
                "failed",
                "none",
            ),
            (
                "timeout failure retry budget",
                {
                    "id": "timeout_failed_retry",
                    "prompt": "Retry",
                    "status": "failed",
                    "failure_category": "timeout",
                    "retryable_failures": ["timeout"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                "failed",
                "retry",
            ),
            (
                "malformed retry budget remains terminal",
                {
                    "id": "malformed_retry_budget",
                    "prompt": "Investigate",
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": "bad",
                    "retry_limit": 1,
                },
                "failed",
                "none",
            ),
        )

        for name, persisted_worker, expected_status, expected_action in cases:
            with self.subTest(name=name):
                snapshot = normalize_worker_snapshot_for_storage(persisted_worker, persisted_worker["id"])
                worker = hydrate_worker_record(snapshot, persisted_worker["id"])
                output = worker.to_output_dict()

                self.assertEqual(output["status"], expected_status)
                self.assertEqual(output["next_eligible_action"], expected_action)
                self.assertNotIn("status", snapshot)
                self.assertNotIn("next_eligible_action", snapshot)

    def test_worker_record_rejects_invalid_canonical_and_output_only_fields(self):
        cases = (
            ({"id": ""}, "worker id"),
            ({"lifecycle_state": "missing"}, "lifecycle_state"),
            ({"retry_count": "1"}, "retry_count"),
            ({"retry_limit": None}, "retry_limit"),
            ({"dependencies": "build"}, "dependencies"),
            ({"attempts": "attempt-1"}, "attempts"),
            ({"timeout_policy": "waiting"}, "timeout_policy"),
            ({"status": "done"}, "output-only"),
            ({"next_eligible_action": "collect"}, "output-only"),
            ({"unknown_plugin_state": {"attempt": 2}}, "unknown worker field"),
        )

        for fields, message in cases:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex((TypeError, ValueError), message):
                    WorkerRecord.from_worker(fields, "review")

    def test_storage_boundary_preserves_unknown_fields_outside_runtime_record(self):
        persisted = {
            "id": "review",
            "role": "reviewer",
            "unknown_plugin_state": {"attempt": 2},
        }

        migrated = migrate_persisted_worker_snapshot(persisted, "review")
        snapshot = normalize_worker_snapshot_for_storage(persisted, "review")
        worker = hydrate_worker_record(snapshot, "review")

        self.assertEqual(migrated["unknown_plugin_state"], {"attempt": 2})
        self.assertEqual(snapshot["unknown_plugin_state"], {"attempt": 2})
        self.assertNotIn("unknown_plugin_state", worker.to_snapshot())
        self.assertFalse(hasattr(worker, "unknown_plugin_state"))

    def test_worker_record_has_no_arbitrary_runtime_fields(self):
        worker = WorkerRecord.default_fields("review")

        with self.assertRaises(AttributeError):
            worker.unknown_plugin_state = {"attempt": 2}


if __name__ == "__main__":
    unittest.main()
