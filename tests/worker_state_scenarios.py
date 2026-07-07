from copy import deepcopy
from dataclasses import dataclass

from opencode_session.worker_state import (
    apply_worker_transition_to_worker,
    normalize_worker,
)


@dataclass(frozen=True)
class WorkerTransitionCase:
    name: str
    worker_fields: object
    transition_factory: object
    expected_outcome: object
    expected_fields: object = None
    absent_fields: object = ()


class WorkerScenario:
    def __init__(self, worker_id="worker", **fields):
        self.worker = normalize_worker(fields, worker_id)

    def apply(self, transition_factory):
        transition = transition_factory(self.worker)
        apply_worker_transition_to_worker(self.worker, transition)
        return self

    def assert_outcome(self, test_case, **expected):
        assert_worker_outcome(test_case, self.worker, **expected)
        return self


def assert_worker_outcome(
    test_case,
    worker,
    *,
    status,
    action,
    lifecycle=None,
    blockers=None,
    output_refs=None,
):
    output = worker.to_output_dict()
    snapshot = worker.to_snapshot()

    test_case.assertEqual(output["status"], status)
    test_case.assertEqual(output["next_eligible_action"], action)
    if lifecycle is not None:
        test_case.assertEqual(snapshot["lifecycle_state"], lifecycle)
    if blockers is not None:
        test_case.assertEqual(snapshot["blockers"], blockers)
    if output_refs is not None:
        test_case.assertEqual(snapshot["output_refs"], output_refs)


def assert_worker_transition_case(test_case, case, *, worker_id="review"):
    worker = normalize_worker(deepcopy(case.worker_fields), worker_id)
    transition = case.transition_factory(worker)
    test_case.assertTrue(transition, case.name)

    apply_worker_transition_to_worker(worker, transition)

    assert_worker_outcome(test_case, worker, **case.expected_outcome)
    snapshot = worker.to_snapshot()
    for field_name, expected_value in (case.expected_fields or {}).items():
        test_case.assertEqual(snapshot.get(field_name), expected_value)
    for field_name in case.absent_fields:
        test_case.assertNotIn(field_name, snapshot)
    return worker
