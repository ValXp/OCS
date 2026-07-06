from copy import deepcopy
from dataclasses import dataclass

from opencode_session.worker_state import (
    apply_worker_transition_to_worker,
    normalize_worker,
    worker_field,
    worker_has_field,
    worker_output_field,
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
    test_case.assertEqual(worker_output_field(worker, "status"), status)
    test_case.assertEqual(worker_output_field(worker, "next_eligible_action"), action)
    if lifecycle is not None:
        test_case.assertEqual(worker_field(worker, "lifecycle_state"), lifecycle)
    if blockers is not None:
        test_case.assertEqual(worker_field(worker, "blockers"), blockers)
    if output_refs is not None:
        test_case.assertEqual(worker_field(worker, "output_refs"), output_refs)


def assert_worker_transition_case(test_case, case, *, worker_id="review"):
    worker = normalize_worker(deepcopy(case.worker_fields), worker_id)
    transition = case.transition_factory(worker)
    test_case.assertTrue(transition, case.name)

    apply_worker_transition_to_worker(worker, transition)

    assert_worker_outcome(test_case, worker, **case.expected_outcome)
    for field_name, expected_value in (case.expected_fields or {}).items():
        test_case.assertEqual(worker_field(worker, field_name), expected_value)
    for field_name in case.absent_fields:
        test_case.assertFalse(worker_has_field(worker, field_name))
    return worker
