from copy import deepcopy
import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
    EXECUTION_POLICY_FAIL_FAST,
    NextEligibleWorkerExecutor,
    schedule_dependency_ordered_tick,
)
from opencode_session.run_services import RunCommandService, RunStartRequest
from opencode_session.run_store import RunStore, RunStoreError
from opencode_session.worker_execution import WORKER_SESSION_JOURNAL_FIELD, WorkerExecutionOutcome
from opencode_session.worker_dependencies import analyze_worker_dependencies
from opencode_session.worker_state import apply_worker_transition

try:
    from tests.multi_worker_orchestration_helpers import CAPABILITIES, FakeClient, UNSUPPORTED_CAPABILITIES
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import CAPABILITIES, FakeClient, UNSUPPORTED_CAPABILITIES


class WorkerDependencyAnalysisRegressionTest(unittest.TestCase):
    def test_ready_worker_ids_exclude_worker_blocked_by_partially_completed_cycle(self):
        workers = {
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
        }

        analysis = analyze_worker_dependencies(workers)

        self.assertEqual(analysis.ready_worker_ids, ())
        self.assertEqual(
            analysis.blockers_by_worker_id,
            {"review": ("dependency-cycle:build->review->build",)},
        )

    def test_dependency_blockers_propagate_through_failed_and_missing_chains(self):
        workers = {
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
        }

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
        workers = {
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
        }

        analysis = analyze_worker_dependencies(workers)

        self.assertEqual(analysis.ready_worker_ids, ("retry", "start"))

    def test_schedule_tick_returns_serial_next_worker_and_block_transitions_without_mutation(self):
        workers = {
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
        }

        tick = schedule_dependency_ordered_tick(workers)

        self.assertEqual(tick.next_worker_id, "docs")
        self.assertFalse(hasattr(tick, "eligible_worker_ids"))
        self.assertEqual([transition.worker_id for transition in tick.dependency_blocked_transitions], ["review"])
        self.assertTrue(tick.has_pending_workers)
        self.assertEqual(workers["review"]["status"], "queued")

        latest_workers = {"review": dict(workers["review"])}
        apply_worker_transition(latest_workers, tick.dependency_blocked_transitions[0])

        self.assertEqual(latest_workers["review"]["status"], "blocked")
        self.assertEqual(latest_workers["review"]["blockers"], ["dependency:build"])


class MultiWorkerOrchestrationServiceTest(unittest.TestCase):
    def assert_single_worker_attempt(self, worker, *, status, session_id):
        attempts = worker.get("attempts")
        self.assertIsInstance(attempts, list)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.get("session_id"), session_id)
        self.assertEqual(attempt.get("status"), status)
        return attempt

    def test_next_eligible_worker_executor_delegates_to_core_direct_execution(self):
        run = {
            "workers": {
                "worker": {
                    "id": "worker",
                    "prompt": "Finish the worker task",
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                }
            }
        }
        client = object()

        class DirectCore:
            def __init__(self):
                self.calls = []

            def execute_worker(
                self,
                client,
                run,
                worker,
                prompt,
                capabilities,
                *,
                session_id=None,
                agent=None,
                model=None,
                cleanup_requested=False,
                stop_after_retry=False,
            ):
                self.calls.append(
                    {
                        "client": client,
                        "run": run,
                        "worker": worker,
                        "prompt": prompt,
                        "capabilities": capabilities,
                        "session_id": session_id,
                        "agent": agent,
                        "model": model,
                        "cleanup_requested": cleanup_requested,
                        "stop_after_retry": stop_after_retry,
                    }
                )
                updated_run = deepcopy(run)
                updated_run["workers"][worker["id"]]["status"] = "done"
                return WorkerExecutionOutcome("completed", run=updated_run)

        class RecordingSessionTracker:
            def __init__(self):
                self.remembered = []

            def remember_worker_outcome(self, run, fallback_worker, outcome):
                self.remembered.append((run, fallback_worker, outcome.kind))

        core = DirectCore()
        session_tracker = RecordingSessionTracker()

        outcome = NextEligibleWorkerExecutor(core).execute_next(
            run,
            "worker",
            client,
            CAPABILITIES,
            session_tracker=session_tracker,
            execution_policy=EXECUTION_POLICY_FAIL_FAST,
        )

        self.assertEqual(outcome.run["workers"]["worker"]["status"], "done")
        self.assertEqual(len(core.calls), 1)
        self.assertIs(core.calls[0]["client"], client)
        self.assertIs(core.calls[0]["run"], run)
        self.assertIs(core.calls[0]["worker"], run["workers"]["worker"])
        self.assertEqual(core.calls[0]["prompt"], "Finish the worker task")
        self.assertEqual(core.calls[0]["capabilities"], CAPABILITIES)
        self.assertIsNone(core.calls[0]["session_id"])
        self.assertEqual(core.calls[0]["agent"], "build")
        self.assertEqual(core.calls[0]["model"], "openai/gpt-5.5")
        self.assertFalse(core.calls[0]["cleanup_requested"])
        self.assertTrue(core.calls[0]["stop_after_retry"])
        self.assertEqual(session_tracker.remembered[0][1]["status"], "done")
        self.assertEqual(session_tracker.remembered[0][2], "completed")

    def test_start_persists_active_attempt_before_provider_call(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "worker", role="worker", prompt="Finish the worker task")
            client = FakeClient(["ses_initial"])
            observed_before_call = {}

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                persisted_worker = store.load_run("demo")["workers"]["worker"]
                observed_before_call["worker"] = deepcopy(persisted_worker)

                self.assertEqual(persisted_worker["session_id"], "ses_initial")
                self.assertEqual(persisted_worker["status"], "active")
                self.assertEqual(persisted_worker["next_eligible_action"], "wait")
                attempt = self.assert_single_worker_attempt(
                    persisted_worker,
                    status="active",
                    session_id="ses_initial",
                )
                self.assertEqual(attempt.get("id"), "attempt-1")
                self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
                self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
                self.assertIsNone(attempt.get("finished_at"))
                self.assertNotIn("result_status", attempt)
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("worker", observed_before_call)
        self.assertEqual(
            client.requests,
            [("create", directory, None, None), ("execute", "ses_initial", "Finish the worker task")],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["next_eligible_action"], "collect")
        attempt = self.assert_single_worker_attempt(worker, status="completed", session_id="ses_initial")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
        self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("finished_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")

    def test_start_persists_worker_session_creation_intent_before_remote_create(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                agent="build",
                model="openai/gpt-5.5",
            )
            observed_intent = {}
            test_case = self

            class InspectingCreateClient(FakeClient):
                def create_session_response(self, directory, *, agent=None, model=None):
                    persisted_run = store.load_run("demo")
                    journal = persisted_run[WORKER_SESSION_JOURNAL_FIELD]
                    test_case.assertEqual(len(journal), 1)
                    observed_intent["entry"] = deepcopy(journal[0])
                    return super().create_session_response(directory, agent=agent, model=model)

            client = InspectingCreateClient(["ses_initial"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [("create", directory, "build", "openai/gpt-5.5"), ("execute", "ses_initial", "Finish the worker task")],
        )
        entry = observed_intent["entry"]
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["status"], "intent")
        self.assertEqual(entry["worker_id"], "worker")
        self.assertEqual(entry["directory"], directory)
        self.assertEqual(entry["agent"], "build")
        self.assertEqual(entry["model"], "openai/gpt-5.5")
        self.assertFalse(entry["cleanup_requested"])
        self.assertNotIn("session_id", entry)
        self.assertNotIn(WORKER_SESSION_JOURNAL_FIELD, run)

    def test_start_persistence_failure_after_session_creation_leaves_cleanup_metadata(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = RunStore(store_root)
            inner_store.create_run("demo", directory=directory, server_url="http://opencode.example")
            inner_store.upsert_worker("demo", "worker", role="worker", prompt="Finish the worker task")
            store = FailAfterCreatedSessionJournalStore(inner_store)
            client = FakeClient(["ses_initial"])
            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("worker should not execute after persistence failure"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            with self.assertRaisesRegex(RunStoreError, "forced update failure after session creation"):
                service.start(
                    DependencyOrderedSerialRunStartRequest(
                        name="demo",
                        worker_id="worker",
                        role="worker",
                        cleanup=True,
                    )
                )
            run = inner_store.load_run("demo")

        self.assertEqual(client.requests, [("create", directory, None, None)])
        self.assertTrue(store.failed)
        journal = run[WORKER_SESSION_JOURNAL_FIELD]
        self.assertEqual(len(journal), 1)
        entry = journal[0]
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["status"], "created")
        self.assertEqual(entry["worker_id"], "worker")
        self.assertEqual(entry["session_id"], "ses_initial")
        self.assertEqual(entry["created_session_ids"], ["ses_initial"])
        self.assertTrue(entry["cleanup_requested"])
        worker = run["workers"]["worker"]
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": False, "sessions": ["ses_initial"]})

    def test_command_service_start_passes_injected_dependencies_to_orchestration(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "worker", role="worker", prompt="Finish the worker task")
            client = FakeClient([])
            detected_clients = []

            def detect_capabilities(detected_client):
                detected_clients.append(detected_client)
                return UNSUPPORTED_CAPABILITIES

            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start_run(RunStartRequest(name="demo", worker_id="worker", role="worker"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 70)
        self.assertEqual(detected_clients, [client])
        self.assertEqual(run["updated_at"], "2026-07-03T00:00:00Z")

    def test_start_unsupported_blocking_execution_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                retry_limit=1,
                retryable_failures=["api"],
            )
            client = FakeClient([])
            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: UNSUPPORTED_CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("unsupported server should not execute worker"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 70)
        self.assertIn("unsupported route behavior", outcome.error)
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_category"], "api")
        self.assertEqual(worker["retryable_failures"], ["api"])
        self.assertEqual(worker["next_eligible_action"], "none")

    def test_start_api_setup_failure_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                retry_limit=1,
                retryable_failures=["api"],
            )
            client = FakeClient([])

            def detect_capabilities(client):
                raise OpenCodeApiError("capability probe failed")

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("failed setup should not execute worker"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "api failure: capability probe failed")
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_category"], "api")
        self.assertEqual(worker["retryable_failures"], ["api"])
        self.assertEqual(worker["next_eligible_action"], "none")

    def test_start_keeps_failed_dependency_blocker_when_capability_probe_fails(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
                status="failed",
            )
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["build"],
            )
            store.upsert_worker(
                "demo",
                "docs",
                role="write",
                prompt="Draft the release notes",
            )
            client = FakeClient([])
            detector_calls = []

            def detect_capabilities(client):
                detector_calls.append(client)
                raise OpenCodeApiError("capability probe failed")

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "api failure: capability probe failed")
        self.assertEqual(detector_calls, [client])
        self.assertEqual(client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workers"]["build"]["status"], "failed")
        self.assertEqual(run["workers"]["docs"]["status"], "failed")
        self.assertEqual(run["workers"]["docs"]["failure_category"], "api")
        review = run["workers"]["review"]
        self.assertEqual(review["status"], "blocked")
        self.assertEqual(review["blockers"], ["dependency:build"])
        self.assertEqual(review["next_eligible_action"], "resolve_blocker")
        self.assertIsNone(review.get("failure_category"))
        self.assertIsNone(review.get("error"))

    def test_start_keeps_missing_dependency_blocker_when_capability_probe_fails(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["build"],
            )
            store.upsert_worker(
                "demo",
                "docs",
                role="write",
                prompt="Draft the release notes",
            )
            client = FakeClient([])
            detector_calls = []

            def detect_capabilities(client):
                detector_calls.append(client)
                raise OpenCodeApiError("capability probe failed")

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "api failure: capability probe failed")
        self.assertEqual(detector_calls, [client])
        self.assertEqual(client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workers"]["docs"]["status"], "failed")
        self.assertEqual(run["workers"]["docs"]["failure_category"], "api")
        review = run["workers"]["review"]
        self.assertEqual(review["status"], "blocked")
        self.assertEqual(review["blockers"], ["dependency:build"])
        self.assertEqual(review["next_eligible_action"], "resolve_blocker")
        self.assertIsNone(review.get("failure_category"))
        self.assertIsNone(review.get("error"))

    def test_start_blocks_failed_and_missing_dependency_chains_before_probe(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
                status="failed",
            )
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["build"],
            )
            store.upsert_worker(
                "demo",
                "deploy",
                role="deploy",
                prompt="Deploy the reviewed implementation",
                dependencies=["review"],
            )
            store.upsert_worker(
                "demo",
                "docs",
                role="write",
                prompt="Draft the docs",
                dependencies=["missing"],
            )
            store.upsert_worker(
                "demo",
                "publish",
                role="publish",
                prompt="Publish the docs",
                dependencies=["docs"],
            )
            client = FakeClient([])
            detector_calls = []

            def detect_capabilities(client):
                detector_calls.append(client)
                return CAPABILITIES

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("locally blocked run should not execute workers"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="deploy", role="deploy"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertIsNone(outcome.error)
        self.assertEqual(detector_calls, [])
        self.assertEqual(client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workers"]["build"]["status"], "failed")
        self.assertEqual(run["workers"]["review"]["status"], "blocked")
        self.assertEqual(run["workers"]["review"]["blockers"], ["dependency:build"])
        self.assertEqual(run["workers"]["review"]["next_eligible_action"], "resolve_blocker")
        self.assertEqual(run["workers"]["deploy"]["status"], "blocked")
        self.assertEqual(run["workers"]["deploy"]["blockers"], ["dependency:review"])
        self.assertEqual(run["workers"]["deploy"]["next_eligible_action"], "resolve_blocker")
        self.assertEqual(run["workers"]["docs"]["status"], "blocked")
        self.assertEqual(run["workers"]["docs"]["blockers"], ["dependency:missing"])
        self.assertEqual(run["workers"]["docs"]["next_eligible_action"], "resolve_blocker")
        self.assertEqual(run["workers"]["publish"]["status"], "blocked")
        self.assertEqual(run["workers"]["publish"]["blockers"], ["dependency:docs"])
        self.assertEqual(run["workers"]["publish"]["next_eligible_action"], "resolve_blocker")

    def test_start_blocks_only_failed_dependency_when_another_dependency_is_done(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "docs",
                role="write",
                prompt="Draft the release notes",
                status="done",
                output_refs=["assistant:msg_docs_assistant"],
            )
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
                status="failed",
            )
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["docs", "build"],
            )
            client = FakeClient([])
            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["output_refs"], ["docs:msg_docs_assistant"])
        self.assertEqual(run["workers"]["docs"]["status"], "done")
        self.assertEqual(run["workers"]["build"]["status"], "failed")
        self.assertEqual(run["workers"]["review"]["status"], "blocked")
        self.assertEqual(run["workers"]["review"]["blockers"], ["dependency:build"])
        self.assertEqual(run["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_continue_policy_runs_independent_ready_worker_after_failure(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "alpha", role="build", prompt="Run alpha")
            store.upsert_worker("demo", "beta", role="write", prompt="Run beta")
            client = FakeClient(["ses_alpha", "ses_beta"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                if session_id == "ses_alpha":
                    raise BlockingProviderFailure("alpha failed", prompt_id="msg_alpha_user")
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_beta_user", "assistant": "msg_beta_assistant"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.02,
                    "tokens": {"total": 12},
                    "text": "Beta finished.",
                }

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                DependencyOrderedSerialRunStartRequest(
                    name="demo",
                    worker_id="alpha",
                    role="build",
                    execution_policy="continue",
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.error, "provider failure: alpha failed")
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_alpha", "Run alpha"),
                ("create", directory, None, None),
                ("execute", "ses_beta", "Run beta"),
            ],
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["status"], "failed")
        self.assertEqual(run["workers"]["beta"]["status"], "done")
        self.assertEqual(run["workers"]["beta"]["output_refs"], ["assistant:msg_beta_assistant"])

    def test_start_does_not_probe_capabilities_when_partially_completed_cycle_blocks_worker(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
                status="done",
                dependencies=["review"],
                output_refs=["assistant:msg_build_assistant"],
            )
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["build"],
            )
            client = FakeClient([])
            detector_calls = []
            executions = []

            def detect_capabilities(client):
                detector_calls.append(client)
                return CAPABILITIES

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                self.fail("locally blocked run should not execute workers")

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 75)
        self.assertEqual(detector_calls, [])
        self.assertEqual(client.requests, [])
        self.assertEqual(executions, [])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["workers"]["build"]["status"], "done")
        self.assertEqual(run["workers"]["review"]["status"], "blocked")
        self.assertEqual(run["workers"]["review"]["blockers"], ["dependency-cycle:build->review->build"])
        self.assertEqual(run["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_does_not_execute_blocked_worker_after_dependency_succeeds(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
            )
            client = FakeClient(["ses_build"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                if prompt == "Review the implementation":
                    self.fail("blocked worker should not execute without requeue")
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_build_user", "assistant": "msg_build_assistant"},
                    "status": "done",
                }

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            first_outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="build", role="build"))
            requests_after_first_start = list(client.requests)
            executions_after_first_start = list(executions)
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                session_id="ses_review",
                dependencies=["build"],
                status="blocked",
                blockers=["manual:blocker"],
            )

            second_outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(first_outcome.exit_code, 0)
        self.assertEqual(second_outcome.exit_code, 75)
        self.assertEqual(requests_after_first_start, [("create", directory, None, None)])
        self.assertEqual(executions_after_first_start, [("ses_build", "Run the implementation")])
        self.assertEqual(client.requests, requests_after_first_start)
        self.assertEqual(executions, executions_after_first_start)
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["output_refs"], ["build:msg_build_assistant"])
        self.assertEqual(run["workers"]["build"]["status"], "done")
        self.assertEqual(run["workers"]["review"]["status"], "blocked")
        self.assertEqual(run["workers"]["review"]["blockers"], ["manual:blocker"])
        self.assertEqual(run["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_requeued_worker_finishes_without_stale_status_metadata(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "build",
                role="build",
                prompt="Run the implementation",
                status="done",
                output_refs=["assistant:msg_build_assistant"],
            )
            store.upsert_worker(
                "demo",
                "review",
                role="review",
                prompt="Review the implementation",
                session_id="ses_review",
                dependencies=["build"],
                status="queued",
                blockers=["dependency:build"],
            )

            def seed_stale_metadata(run):
                worker = run["workers"]["review"]
                worker["error"] = "previous failure"
                worker["failure_category"] = "api"
                worker["failure_reason"] = "previous failure"
                worker["failure_retryable"] = False
                worker["last_failure_category"] = "api"
                worker["last_failure_reason"] = "previous failure"

            store.update_run("demo", seed_stale_metadata)
            client = FakeClient([])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_review_user", "assistant": "msg_review_assistant"},
                    "status": "done",
                }

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(DependencyOrderedSerialRunStartRequest(name="demo", worker_id="review", role="review"))
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, [("ses_review", "Review the implementation")])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["output_refs"], ["build:msg_build_assistant", "review:msg_review_assistant"])
        review = run["workers"]["review"]
        self.assertEqual(review["status"], "done")
        self.assertEqual(review["blockers"], [])
        self.assertNotIn("error", review)
        self.assertIsNone(review["failure_category"])
        self.assertIsNone(review["failure_reason"])
        self.assertNotIn("failure_retryable", review)
        self.assertEqual(review["last_failure_category"], "api")
        self.assertEqual(review["last_failure_reason"], "previous failure")
        self.assertEqual(review["next_eligible_action"], "collect")


class FailAfterCreatedSessionJournalStore:
    def __init__(self, store):
        self.store = store
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.store, name)

    def update_run(self, name, mutator):
        def fail_after_created_journal(run):
            should_fail = not self.failed and _has_created_session_journal(run)
            result = mutator(run)
            if should_fail:
                self.failed = True
                raise RunStoreError("forced update failure after session creation")
            return result

        return self.store.update_run(name, fail_after_created_journal)


def _has_created_session_journal(run):
    journal = run.get(WORKER_SESSION_JOURNAL_FIELD)
    if not isinstance(journal, list):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("kind") == "worker_session_create"
        and entry.get("status") == "created"
        for entry in journal
    )


if __name__ == "__main__":
    unittest.main()
