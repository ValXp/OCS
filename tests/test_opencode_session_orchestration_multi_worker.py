import unittest

from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunLoopPlanner,
    DependencyOrderedSerialStep,
    SerialWorkerExecutionOutcome,
    plan_dependency_ordered_serial_step,
)
from opencode_session.run_persistence import persist_worker_transitions
from opencode_session.worker_execution import WorkerExecutionOutcome
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import (
    WorkerTransitionError,
    apply_worker_transition,
    mark_worker_active,
    normalize_worker,
    worker_field,
    worker_output_field,
)

try:
    from tests.multi_worker_orchestration_helpers import NOW, DependencyOrderedSerialServiceScenario
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import NOW, DependencyOrderedSerialServiceScenario


class WorkerDependencyAnalysisRegressionTest(unittest.TestCase):
    def test_ready_worker_ids_exclude_worker_blocked_by_partially_completed_cycle(self):
        workers = _hydrated_workers({
            "build": {
                "id": "build",
                "prompt": "Run the implementation",
                "lifecycle_state": "done_collect",
                "status": "done",
                "dependencies": ["review"],
            },
            "review": {
                "id": "review",
                "prompt": "Review the implementation",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["build"],
            },
        })

        analysis = analyze_worker_dependencies(workers)

        self.assertEqual(analysis.ready_worker_ids, ())
        self.assertEqual(
            analysis.blockers_by_worker_id,
            {"review": ("dependency-cycle:build->review->build",)},
        )

    def test_dependency_blockers_propagate_through_failed_and_missing_chains(self):
        workers = _hydrated_workers({
            "deploy": {
                "id": "deploy",
                "prompt": "Deploy the reviewed implementation",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["review"],
            },
            "review": {
                "id": "review",
                "prompt": "Review the implementation",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["build"],
            },
            "build": {
                "id": "build",
                "prompt": "Run the implementation",
                "lifecycle_state": "failed_terminal",
                "status": "failed",
            },
            "publish": {
                "id": "publish",
                "prompt": "Publish the docs",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["docs"],
            },
            "docs": {
                "id": "docs",
                "prompt": "Draft the docs",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["missing"],
            },
        })

        analysis = analyze_worker_dependencies(workers)

        expected_blockers = {
            "deploy": ("dependency:review",),
            "docs": ("dependency:missing",),
            "publish": ("dependency:docs",),
            "review": ("dependency:build",),
        }
        self.assertEqual(analysis.ready_worker_ids, ())
        self.assertEqual(analysis.invalid_graph_blockers_by_worker_id, {})
        self.assertEqual(analysis.dependency_blockers_by_worker_id, expected_blockers)
        self.assertEqual(analysis.blockers_by_worker_id, expected_blockers)

    def test_ready_worker_ids_use_next_eligible_action_for_active_workers(self):
        workers = _hydrated_workers({
            "retry": {
                "id": "retry",
                "prompt": "Retry transient failure",
                "lifecycle_state": "active_retry",
                "status": "active",
                "next_eligible_action": "wait",
            },
            "start": {
                "id": "start",
                "prompt": "Start queued worker",
                "lifecycle_state": "queued",
                "status": "queued",
            },
            "wait": {
                "id": "wait",
                "prompt": "Wait for existing worker",
                "lifecycle_state": "active_wait",
                "status": "active",
                "next_eligible_action": "retry",
            },
        })

        analysis = analyze_worker_dependencies(workers)

        self.assertEqual(analysis.ready_worker_ids, ("retry", "start"))

    def test_dependency_ordered_serial_step_selects_one_ready_worker_and_blocks_without_mutation(self):
        workers = _hydrated_workers({
            "build": {"id": "build", "prompt": "Build", "lifecycle_state": "failed_terminal", "status": "failed"},
            "docs": {"id": "docs", "prompt": "Docs", "lifecycle_state": "queued", "status": "queued"},
            "lint": {"id": "lint", "prompt": "Lint", "lifecycle_state": "queued", "status": "queued"},
            "review": {
                "id": "review",
                "prompt": "Review",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["build"],
            },
        })

        analysis = analyze_worker_dependencies(workers)
        step = plan_dependency_ordered_serial_step(workers)

        self.assertEqual(analysis.ready_worker_ids, ("docs", "lint"))
        self.assertEqual(step.worker_id, "docs")
        self.assertFalse(hasattr(step, "ready_worker_ids"))
        self.assertFalse(hasattr(step, "eligible_worker_ids"))
        self.assertEqual([transition.worker_id for transition in step.dependency_blocked_transitions], ["review"])
        self.assertEqual(worker_output_field(workers["review"], "status"), "queued")

        latest_workers = {"review": normalize_worker(workers["review"].to_snapshot(), "review")}
        apply_worker_transition(latest_workers, step.dependency_blocked_transitions[0])

        self.assertEqual(worker_output_field(latest_workers["review"], "status"), "blocked")
        self.assertEqual(worker_field(latest_workers["review"], "blockers"), ["dependency:build"])

    def test_persist_worker_transitions_rejects_illegal_transition_with_reason(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker("build", prompt="Build", status="done")
            run = scenario.load_run()
            transition = mark_worker_active(run["workers"]["build"])

            with self.assertRaisesRegex(WorkerTransitionError, "illegal worker transition 'active'") as raised:
                persist_worker_transitions(
                    scenario.store,
                    run,
                    [transition],
                    refresh_run_summary=lambda run: None,
                    now=lambda: NOW,
                )
            persisted = scenario.load_run()

        self.assertIn("from lifecycle_state 'done_collect'", raised.exception.result.reason)
        self.assertEqual(worker_output_field(persisted["workers"]["build"], "status"), "done")

    def test_dependency_ordered_serial_step_advances_one_worker_at_a_time_as_dependencies_finish(self):
        workers = _hydrated_workers({
            "build": {"id": "build", "prompt": "Build", "lifecycle_state": "queued", "status": "queued"},
            "review": {
                "id": "review",
                "prompt": "Review",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["build"],
            },
            "deploy": {
                "id": "deploy",
                "prompt": "Deploy",
                "lifecycle_state": "queued",
                "status": "queued",
                "dependencies": ["review"],
            },
        })

        first_step = plan_dependency_ordered_serial_step(workers)
        workers["build"].update_canonical_fields(lifecycle_state="done_collect")
        second_step = plan_dependency_ordered_serial_step(workers)
        workers["review"].update_canonical_fields(lifecycle_state="done_collect")
        third_step = plan_dependency_ordered_serial_step(workers)
        workers["deploy"].update_canonical_fields(lifecycle_state="done_collect")
        final_step = plan_dependency_ordered_serial_step(workers)

        self.assertEqual(first_step.worker_id, "build")
        self.assertEqual(second_step.worker_id, "review")
        self.assertEqual(third_step.worker_id, "deploy")
        self.assertIsNone(final_step.worker_id)

    def test_serial_run_loop_planner_keeps_next_worker_policy_pure(self):
        planner = DependencyOrderedSerialRunLoopPlanner()
        first_step = DependencyOrderedSerialStep("alpha", (), {})
        next_step = DependencyOrderedSerialStep("beta", (), {})
        empty_step = DependencyOrderedSerialStep(None, (), {})
        failure = WorkerExecutionOutcome("failed", error="provider failure: alpha failed")

        first_plan = planner.initial_plan(first_step)
        continue_plan = planner.after_worker(
            first_plan,
            SerialWorkerExecutionOutcome({"name": "demo"}, failure),
            next_step,
        )
        fail_fast_plan = planner.after_worker(
            first_plan,
            SerialWorkerExecutionOutcome({"name": "demo"}, failure, failure),
            next_step,
        )

        self.assertEqual(first_plan.worker_id, "alpha")
        self.assertIsNone(planner.initial_plan(empty_step).worker_id)
        self.assertEqual(continue_plan.worker_id, "beta")
        self.assertIs(continue_plan.first_error_outcome, failure)
        self.assertIsNone(continue_plan.fail_fast_outcome)
        self.assertIsNone(fail_fast_plan.worker_id)
        self.assertIs(fail_fast_plan.first_error_outcome, failure)
        self.assertIs(fail_fast_plan.fail_fast_outcome, failure)


def _hydrated_workers(workers):
    return {worker_id: hydrate_worker_record(worker, worker_id) for worker_id, worker in workers.items()}


if __name__ == "__main__":
    unittest.main()
