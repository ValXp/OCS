import json
import unittest
from typing import Dict, get_type_hints

from opencode_session.run_record import normalize_run, normalize_run_for_storage, run_record_for_output
from opencode_session.schema_common import (
    HydratedRunRecord,
    HydratedWorker,
    PersistedRunRecord,
    RunRecord,
    WorkerSnapshotRecord,
)
from opencode_session.worker_state import WorkerRecord, worker_output_field


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
        self.assertEqual(worker.field("id"), "review")
        self.assertIsNone(worker.field("status"))
        self.assertIsNone(worker.field("next_eligible_action"))
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
        self.assertIsNone(worker.field("status"))
        self.assertIsNone(worker.field("next_eligible_action"))
        self.assertEqual(output["status"], "active")
        self.assertEqual(output["next_eligible_action"], "retry")

    def test_worker_state_core_does_not_infer_legacy_public_status(self):
        from opencode_session.worker_state import normalize_worker, worker_lifecycle_state

        worker = normalize_worker(
            {
                "id": "review",
                "role": "review",
                "prompt": "Review the change",
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(worker_lifecycle_state(worker), "queued")
        self.assertIsNone(worker.field("status"))
        self.assertIsNone(worker.field("next_eligible_action"))

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

        self.assertIsNone(worker.field("status"))
        self.assertIsNone(worker.field("next_eligible_action"))
        self.assertNotIn("status", worker.to_snapshot())
        self.assertNotIn("next_eligible_action", worker.to_snapshot())
        self.assertEqual(output_worker["lifecycle_state"], "active_wait")
        self.assertEqual(output_worker["status"], "active")
        self.assertEqual(output_worker["next_eligible_action"], "wait")


if __name__ == "__main__":
    unittest.main()
