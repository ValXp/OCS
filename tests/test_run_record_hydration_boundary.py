from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from typing import Dict, get_type_hints

from opencode_session.commands.rendering import render_command_result
from opencode_session.run_record import normalize_run, normalize_run_for_storage, run_record_for_output
from opencode_session.schema_run import HydratedRunRecord, PersistedRunRecord, RunRecord
from opencode_session.schema_worker import HydratedWorker, WorkerSnapshotRecord
from opencode_session.worker_storage_adapter import migrate_persisted_worker_snapshot, normalize_worker_snapshot_for_storage
from opencode_session.worker_state import WorkerRecord, worker_field, worker_output_field


class RunRecordHydrationBoundaryTest(unittest.TestCase):
    def test_schema_types_separate_hydrated_and_persisted_workers(self):
        self.assertIs(RunRecord, HydratedRunRecord)
        self.assertEqual(get_type_hints(HydratedRunRecord)["schema_version"], int)
        self.assertEqual(get_type_hints(PersistedRunRecord)["schema_version"], int)
        self.assertEqual(get_type_hints(HydratedRunRecord)["workers"], Dict[str, HydratedWorker])
        self.assertEqual(get_type_hints(PersistedRunRecord)["workers"], Dict[str, WorkerSnapshotRecord])

    def test_normalize_run_hydrates_workers_as_worker_records(self):
        run = normalize_run(
            {
                "name": "demo",
                "workers": {
                    "review": {
                        "id": "review",
                        "role": "review",
                        "prompt": "Review the change",
                        "lifecycle_state": "active_wait",
                        "status": "done",
                        "next_eligible_action": "collect",
                    }
                },
            },
            fallback_name="demo",
        )

        worker = run["workers"]["review"]

        self.assertIsInstance(worker, WorkerRecord)
        self.assertNotIsInstance(worker, dict)
        self.assertEqual(worker.worker_id, "review")
        self.assertIsNone(worker_field(worker, "status"))
        self.assertIsNone(worker_field(worker, "next_eligible_action"))
        self.assertEqual(worker.lifecycle_state, "active_wait")
        self.assertEqual(worker_output_field(worker, "status"), "active")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "wait")

    def test_normalize_run_migrates_legacy_public_status_at_hydration_boundary(self):
        run = normalize_run(
            {
                "name": "demo",
                "workers": {
                    "review": {
                        "id": "review",
                        "role": "review",
                        "prompt": "Review the change",
                        "status": "active",
                        "next_eligible_action": "retry",
                    }
                },
            },
            fallback_name="demo",
        )

        worker = run["workers"]["review"]
        output = run_record_for_output(run)["workers"]["review"]

        self.assertIsInstance(worker, WorkerRecord)
        self.assertEqual(worker.lifecycle_state, "active_retry")
        self.assertIsNone(worker_field(worker, "status"))
        self.assertIsNone(worker_field(worker, "next_eligible_action"))
        self.assertEqual(output["status"], "active")
        self.assertEqual(output["next_eligible_action"], "retry")

    def test_versioned_worker_snapshot_migration_runs_before_hydration(self):
        legacy_worker = {
            "id": "",
            "role": "review",
            "status": "failed",
            "next_eligible_action": "retry",
            "failure_category": "provider",
            "retryable_failures": ["provider"],
            "retry_count": "0",
            "retry_limit": "1",
            "dependencies": "build",
            "timeout_policy": "custom",
        }

        migrated = migrate_persisted_worker_snapshot(legacy_worker, "review", run_schema_version=1)
        run = normalize_run(
            {
                "schema_version": 1,
                "name": "demo",
                "workers": {"review": legacy_worker},
            },
            fallback_name="demo",
        )
        worker = run["workers"]["review"]
        snapshot = worker.to_snapshot()
        output = run_record_for_output(run)["workers"]["review"]

        self.assertEqual(migrated["id"], "review")
        self.assertEqual(migrated["lifecycle_state"], "failed_retry")
        self.assertEqual(migrated["retry_count"], 0)
        self.assertEqual(migrated["retry_limit"], 1)
        self.assertEqual(migrated["dependencies"], [])
        self.assertEqual(migrated["timeout_policy"], "timeout")
        self.assertNotIn("status", migrated)
        self.assertNotIn("next_eligible_action", migrated)
        self.assertIsInstance(worker, WorkerRecord)
        self.assertEqual(worker.worker_id, "review")
        self.assertEqual(worker.lifecycle_state, "failed_retry")
        self.assertEqual(worker.retry_count, 0)
        self.assertEqual(worker.retry_limit, 1)
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["next_eligible_action"], "retry")

    def test_versioned_worker_snapshot_migration_is_plain_data_transformation(self):
        legacy_worker = {
            "id": "",
            "role": "review",
            "status": "failed",
            "failure_category": "provider",
            "retryable_failures": ["provider"],
            "retry_count": "0",
            "retry_limit": "1",
            "dependencies": "build",
            "unknown_plugin_state": {"attempt": 2},
        }

        with patch(
            "opencode_session.worker_storage_adapter.WorkerRecord.from_worker",
            side_effect=AssertionError("migration must not hydrate runtime workers"),
        ):
            migrated = migrate_persisted_worker_snapshot(legacy_worker, "review", run_schema_version=1)
            stored = normalize_worker_snapshot_for_storage(legacy_worker, "review", run_schema_version=1)

        self.assertIs(type(migrated), dict)
        self.assertIs(type(stored), dict)
        self.assertEqual(migrated["id"], "review")
        self.assertEqual(migrated["lifecycle_state"], "failed_retry")
        self.assertEqual(migrated["dependencies"], [])
        self.assertEqual(migrated["unknown_plugin_state"], {"attempt": 2})
        self.assertEqual(stored["lifecycle_state"], "failed_retry")
        self.assertEqual(stored["retry_count"], 0)
        self.assertEqual(stored["retry_limit"], 1)
        self.assertEqual(stored["unknown_plugin_state"], {"attempt": 2})
        self.assertNotIn("status", stored)

    def test_worker_state_core_rejects_legacy_public_status(self):
        from opencode_session.worker_state import normalize_worker

        with self.assertRaisesRegex(ValueError, "output-only"):
            normalize_worker(
                {
                    "id": "review",
                    "role": "review",
                    "prompt": "Review the change",
                    "status": "done",
                    "next_eligible_action": "collect",
                },
                "review",
            )

    def test_storage_normalization_serializes_plain_json_worker_snapshots(self):
        hydrated = normalize_run(
            {
                "name": "demo",
                "workers": {
                    "review": {
                        "id": "review",
                        "role": "review",
                        "prompt": "Review the change",
                        "lifecycle_state": "active_wait",
                        "status": "done",
                        "next_eligible_action": "collect",
                    }
                },
            },
            fallback_name="demo",
        )

        stored = normalize_run_for_storage(hydrated, fallback_name="demo")
        snapshot = stored["workers"]["review"]
        round_tripped = json.loads(json.dumps(stored))

        self.assertIs(type(snapshot), dict)
        self.assertNotIsInstance(snapshot, WorkerRecord)
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertEqual(snapshot["prompt"], "Review the change")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)
        self.assertIs(type(round_tripped["workers"]["review"]), dict)

    def test_storage_normalization_does_not_hydrate_workers_for_serialization(self):
        worker = WorkerRecord.default_fields("review")
        worker.update_canonical_fields(
            role="review",
            prompt="Review the change",
            lifecycle_state="active_wait",
        )

        with patch(
            "opencode_session.run_record.hydrate_worker_record",
            side_effect=AssertionError("storage serialization must not hydrate workers"),
        ):
            stored = normalize_run_for_storage(
                {"name": "demo", "workers": {"review": worker}},
                fallback_name="demo",
            )

        snapshot = stored["workers"]["review"]
        self.assertIs(type(snapshot), dict)
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_run_output_projection_adds_public_worker_state_at_output_boundary(self):
        run = normalize_run(
            {
                "name": "demo",
                "workers": {
                    "review": {
                        "id": "review",
                        "role": "review",
                        "lifecycle_state": "active_wait",
                        "status": "done",
                        "next_eligible_action": "collect",
                    }
                },
            },
            fallback_name="demo",
        )
        worker = run["workers"]["review"]

        output = run_record_for_output(run)
        output_worker = output["workers"]["review"]

        self.assertIsNone(worker_field(worker, "status"))
        self.assertIsNone(worker_field(worker, "next_eligible_action"))
        self.assertNotIn("status", worker.to_snapshot())
        self.assertNotIn("next_eligible_action", worker.to_snapshot())
        self.assertEqual(output_worker["lifecycle_state"], "active_wait")
        self.assertEqual(output_worker["status"], "active")
        self.assertEqual(output_worker["next_eligible_action"], "wait")

    def test_worker_record_snapshot_and_output_projection_are_distinct(self):
        worker = WorkerRecord.default_fields("review")
        worker.update_canonical_fields(prompt="Review the change", lifecycle_state="active_wait")

        snapshot = worker.to_snapshot()
        output = worker.to_output_dict()

        self.assertFalse(hasattr(worker, "to_public_dict"))
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)
        self.assertEqual(output["lifecycle_state"], "active_wait")
        self.assertEqual(output["status"], "active")
        self.assertEqual(output["next_eligible_action"], "wait")

    def test_command_json_rendering_uses_worker_output_projection(self):
        worker = WorkerRecord.default_fields("review")
        worker.update_canonical_fields(lifecycle_state="active_wait")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = render_command_result(SimpleNamespace(json=True, raw=False), worker)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["lifecycle_state"], "active_wait")
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["next_eligible_action"], "wait")


if __name__ == "__main__":
    unittest.main()
