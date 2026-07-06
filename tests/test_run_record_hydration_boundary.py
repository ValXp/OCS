import json
import unittest
from typing import Dict, get_type_hints

from opencode_session.run_record import normalize_run, normalize_run_for_storage
from opencode_session.schema_common import (
    HydratedRunRecord,
    HydratedWorker,
    PersistedRunRecord,
    RunRecord,
    WorkerSnapshotRecord,
)
from opencode_session.worker_state import WorkerRecord


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
        self.assertEqual(worker.status, "active")
        self.assertEqual(worker.next_eligible_action, "wait")

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


if __name__ == "__main__":
    unittest.main()
