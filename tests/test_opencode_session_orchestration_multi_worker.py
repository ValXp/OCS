import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.cli_policy import EX_TIMEOUT, EX_UNAVAILABLE, EX_UNSUPPORTED
from opencode_session.multi_worker_execution_outcome import (
    DependencyOrderedSerialExecutionApiFailure,
    DependencyOrderedSerialExecutionCompleted,
    DependencyOrderedSerialExecutionFailFast,
    DependencyOrderedSerialExecutionResult,
    DependencyOrderedSerialExecutionUnsupported,
)
from opencode_session.multi_worker_orchestration import (
    EXECUTION_POLICY_CONTINUE,
    EXECUTION_POLICY_FAIL_FAST,
    DependencyOrderedSerialCleanupRequest,
    DependencyOrderedSerialExecutionRequest,
    DependencyOrderedSerialPlanningRequest,
    DependencyOrderedSerialRecoveryRequest,
    DependencyOrderedSerialRunFlowRequest,
    plan_dependency_ordered_serial_step,
)
from opencode_session.run_persistence import persist_worker_transitions
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import (
    WorkerTransitionError,
    apply_worker_transition,
    mark_worker_active,
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

        latest_workers = {"review": workers["review"].to_worker()}
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


class DependencyOrderedSerialPhaseBoundaryTest(unittest.TestCase):
    def test_phase_requests_return_explicit_results_without_service_callbacks(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_docs"]) as scenario:
            scenario.add_worker("build", role="build", prompt="Build", status="failed")
            scenario.add_worker("docs", role="write", prompt="Draft docs")
            scenario.add_worker("review", role="review", prompt="Review", dependencies=["build"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                return {
                    "message_ids": {"user": "msg_docs_user", "assistant": "msg_docs_assistant"},
                    "status": "done",
                }

            service = scenario.service(executor=execute_prompt)
            recovery = service.recovery_phase.recover(DependencyOrderedSerialRecoveryRequest(scenario.load_run()))
            planning = service.planning_phase.plan(DependencyOrderedSerialPlanningRequest(recovery.run))
            execution = service.execution_phase.execute(
                DependencyOrderedSerialExecutionRequest(
                    planning.run,
                    planning.serial_step,
                    cleanup_requested=False,
                    execution_policy=EXECUTION_POLICY_CONTINUE,
                )
            )
            cleanup = service.cleanup_phase.cleanup(
                DependencyOrderedSerialCleanupRequest(
                    execution.cleanup_context.client,
                    execution.run,
                    execution.cleanup_context.created_session_ids_by_worker,
                )
            )

        self.assertIsNone(recovery.error)
        self.assertEqual(planning.serial_step.worker_id, "docs")
        self.assertEqual(worker_output_field(planning.run["workers"]["review"], "status"), "blocked")
        self.assertEqual(executions, [("ses_docs", "Draft docs")])
        self.assertIsNone(execution.cleanup_context.created_session_ids_by_worker)
        self.assertEqual(worker_output_field(cleanup.run["workers"]["docs"], "status"), "done")
        self.assertEqual(worker_output_field(cleanup.run["workers"]["review"], "status"), "blocked")

    def test_run_flow_request_preserves_dependency_ordered_serial_execution(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_build", "ses_review"]) as scenario:
            scenario.add_worker("build", role="build", prompt="Build")
            scenario.add_worker("review", role="review", prompt="Review", dependencies=["build"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                return {
                    "message_ids": {
                        "user": f"msg_{session_id}_user",
                        "assistant": f"msg_{session_id}_assistant",
                    },
                    "status": "done",
                }

            service = scenario.service(executor=execute_prompt)
            outcome = service.run_flow.start(
                DependencyOrderedSerialRunFlowRequest(
                    scenario.load_run(),
                    cleanup_requested=False,
                    execution_policy=EXECUTION_POLICY_FAIL_FAST,
                )
            )
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, [("ses_build", "Build"), ("ses_review", "Review")])
        self.assertEqual(run["status"], "done")
        self.assertEqual(worker_output_field(run["workers"]["build"], "status"), "done")
        self.assertEqual(worker_output_field(run["workers"]["review"], "status"), "done")


class DependencyOrderedSerialExecutionOutcomeTest(unittest.TestCase):
    def test_completed_variant_finishes_from_run_state_and_first_error(self):
        client = object()
        created_session_ids_by_worker = {"worker": ["ses_worker"]}

        result = DependencyOrderedSerialExecutionResult.completed(
            {"status": "active", "workers": {}},
            client,
            created_session_ids_by_worker,
            "provider failure: boom",
        )
        outcome = result.finish_outcome({"status": "failed", "workers": {}}, None)

        self.assertIsInstance(result.outcome, DependencyOrderedSerialExecutionCompleted)
        self.assertIs(result.cleanup_context.client, client)
        self.assertIs(result.cleanup_context.created_session_ids_by_worker, created_session_ids_by_worker)
        self.assertEqual(outcome.exit_code, EX_UNAVAILABLE)
        self.assertEqual(outcome.error, "provider failure: boom")

    def test_unsupported_variant_uses_unsupported_exit_code(self):
        result = DependencyOrderedSerialExecutionResult.unsupported(
            {"status": "failed", "workers": {}},
            "unsupported route behavior: missing blocking execution",
            {},
        )
        outcome = result.finish_outcome({"status": "done", "workers": {}}, None)

        self.assertIsInstance(result.outcome, DependencyOrderedSerialExecutionUnsupported)
        self.assertIsNone(result.cleanup_context.client)
        self.assertEqual(outcome.exit_code, EX_UNSUPPORTED)
        self.assertEqual(outcome.error, "unsupported route behavior: missing blocking execution")

    def test_api_failure_variant_formats_api_error(self):
        client = object()

        result = DependencyOrderedSerialExecutionResult.api_failure(
            {"status": "failed", "workers": {}},
            client,
            {},
            OpenCodeApiError("capability probe failed"),
        )
        outcome = result.finish_outcome({"status": "done", "workers": {}}, None)

        self.assertIsInstance(result.outcome, DependencyOrderedSerialExecutionApiFailure)
        self.assertIs(result.cleanup_context.client, client)
        self.assertEqual(outcome.exit_code, EX_UNAVAILABLE)
        self.assertEqual(outcome.error, "api failure: capability probe failed")

    def test_fail_fast_variant_uses_run_exit_code_and_worker_error(self):
        result = DependencyOrderedSerialExecutionResult.fail_fast(
            {"status": "failed", "workers": {}},
            object(),
            {},
            "worker timed out after 0.01s",
        )
        outcome = result.finish_outcome({"status": "timeout", "workers": {}}, "recovered timeout")

        self.assertIsInstance(result.outcome, DependencyOrderedSerialExecutionFailFast)
        self.assertEqual(outcome.exit_code, EX_TIMEOUT)
        self.assertEqual(outcome.error, "worker timed out after 0.01s")


def _hydrated_workers(workers):
    return {worker_id: hydrate_worker_record(worker, worker_id) for worker_id, worker in workers.items()}


if __name__ == "__main__":
    unittest.main()
