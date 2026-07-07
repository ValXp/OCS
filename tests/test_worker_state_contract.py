import unittest

from opencode_session.worker_storage_adapter import (
    hydrate_worker_record,
    migrate_persisted_worker_snapshot,
    normalize_worker_snapshot_for_storage,
)
from opencode_session.run_record import upsert_worker_record
from opencode_session.schema_worker import (
    WORKER_REQUIRED_FIELD_NAMES as SCHEMA_WORKER_REQUIRED_FIELD_NAMES,
    WorkerRequiredFields,
)
from opencode_session.worker_state import (
    WORKER_FIELD_SPECS,
    WORKER_LIST_FIELDS,
    WORKER_OPTIONAL_LIST_FIELDS,
    WORKER_RECORD_CANONICAL_FIELD_NAMES,
    WORKER_RECORD_OPTIONAL_FIELD_NAMES,
    WORKER_RECORD_UPDATE_FIELD_NAMES,
    WORKER_REQUIRED_FIELD_NAMES,
    WORKER_RUN_UPSERT_FIELD_NAMES,
    WorkerRecord,
    worker_default_snapshot_fields,
)


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

    def test_worker_field_spec_owns_runtime_field_projections(self):
        spec_names = tuple(spec.name for spec in WORKER_FIELD_SPECS)

        self.assertEqual(WORKER_RECORD_CANONICAL_FIELD_NAMES, frozenset(spec_names))
        self.assertEqual(
            WORKER_REQUIRED_FIELD_NAMES,
            tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.required),
        )
        self.assertEqual(
            WORKER_RECORD_OPTIONAL_FIELD_NAMES,
            tuple(spec.name for spec in WORKER_FIELD_SPECS if not spec.required),
        )
        self.assertEqual(
            WORKER_LIST_FIELDS,
            tuple(
                spec.name
                for spec in WORKER_FIELD_SPECS
                if spec.required and spec.validator == "list"
            ),
        )
        self.assertEqual(
            WORKER_OPTIONAL_LIST_FIELDS,
            tuple(
                spec.name
                for spec in WORKER_FIELD_SPECS
                if not spec.required and spec.validator == "list"
            ),
        )
        self.assertEqual(
            WORKER_RECORD_UPDATE_FIELD_NAMES,
            tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.record_update),
        )
        self.assertEqual(
            WORKER_RUN_UPSERT_FIELD_NAMES,
            tuple(spec.name for spec in WORKER_FIELD_SPECS if spec.run_upsert),
        )
        self.assertEqual(
            SCHEMA_WORKER_REQUIRED_FIELD_NAMES,
            tuple(WorkerRequiredFields.__annotations__),
        )
        self.assertEqual(SCHEMA_WORKER_REQUIRED_FIELD_NAMES, WORKER_REQUIRED_FIELD_NAMES)

    def test_worker_defaults_storage_and_output_use_field_spec(self):
        defaults = worker_default_snapshot_fields("review")
        worker = WorkerRecord.default_fields("review")
        stored = normalize_worker_snapshot_for_storage({"id": "review"}, "review")
        output = worker.to_output_dict()

        self.assertEqual(WorkerRecord.default_snapshot_fields("review"), defaults)
        self.assertEqual(stored, defaults)
        for field_name in WORKER_REQUIRED_FIELD_NAMES:
            with self.subTest(field_name=field_name):
                self.assertEqual(output[field_name], defaults[field_name])
        self.assertEqual(output["status"], "queued")
        self.assertEqual(output["next_eligible_action"], "start")

        defaults["dependencies"].append("mutated")
        self.assertEqual(WorkerRecord.default_snapshot_fields("review")["dependencies"], [])

    def test_run_worker_upsert_uses_field_spec_upsert_projection(self):
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

        self.assertEqual(set(WORKER_RUN_UPSERT_FIELD_NAMES), set(upsert_values))
        upsert_worker_record(
            run,
            "review",
            upsert_values,
            now="2026-07-06T00:00:00Z",
        )

        worker = run["workers"]["review"]
        for field_name in WORKER_RUN_UPSERT_FIELD_NAMES:
            with self.subTest(field_name=field_name):
                self.assertEqual(getattr(worker, field_name), upsert_values[field_name])
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
