from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from typing import Dict, get_type_hints

from opencode_session.commands.rendering import render_command_result
from opencode_session.run_record import normalize_run, normalize_run_for_storage, run_record_for_output
from opencode_session.schema_run import HydratedRunRecord, PersistedRunRecord, RunRecord
from opencode_session.schema_worker import HydratedWorker, WorkerSnapshotRecord
from opencode_session.worker_state import WorkerRecord, worker_field, worker_output_field


class RunRecordHydrationBoundaryTest(unittest.TestCase):
    def test_schema_types_separate_hydrated_and_persisted_workers(self):
        self.assertIs(RunRecord, HydratedRunRecord)
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
