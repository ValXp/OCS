from opencode_session.worker_state import apply_worker_transition_to_worker, normalize_worker


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
    test_case.assertEqual(worker["status"], status)
    test_case.assertEqual(worker["next_eligible_action"], action)
    if lifecycle is not None:
        test_case.assertEqual(worker["lifecycle_state"], lifecycle)
    if blockers is not None:
        test_case.assertEqual(worker["blockers"], blockers)
    if output_refs is not None:
        test_case.assertEqual(worker["output_refs"], output_refs)
