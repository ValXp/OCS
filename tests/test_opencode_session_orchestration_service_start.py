from copy import deepcopy
import tempfile
import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialPlanner,
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
    EXECUTION_POLICY_FAIL_FAST,
    SelectedSerialWorkerExecutor,
)
from opencode_session.run_services import RunCommandService, RunStartRequest
from opencode_session.run_store import RunStore, RunStoreError
from opencode_session.worker_execution import WorkerExecutionOutcome
from opencode_session.worker_session_provisioning import WORKER_SESSION_JOURNAL_FIELD
from opencode_session.worker_state import normalize_worker, worker_field, worker_output_field

try:
    from tests.multi_worker_orchestration_helpers import (
        CAPABILITIES,
        NOW,
        RUN_NAME,
        SERVER_URL,
        UNSUPPORTED_CAPABILITIES,
        DependencyOrderedSerialServiceScenario,
        FakeClient,
        assert_single_worker_attempt,
    )
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import (
        CAPABILITIES,
        NOW,
        RUN_NAME,
        SERVER_URL,
        UNSUPPORTED_CAPABILITIES,
        DependencyOrderedSerialServiceScenario,
        FakeClient,
        assert_single_worker_attempt,
    )


class DependencyOrderedSerialOrchestrationServiceStartTest(unittest.TestCase):
    def test_next_eligible_worker_executor_delegates_to_core_direct_execution(self):
        run = {
            "workers": {
                "worker": normalize_worker(
                    {
                        "id": "worker",
                        "prompt": "Finish the worker task",
                        "agent": "build",
                        "model": "openai/gpt-5.5",
                    },
                    "worker",
                )
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
                    }
                )
                updated_run = deepcopy(run)
                updated_run["workers"][worker_field(worker, "id")].set_field("lifecycle_state", "done_collect")
                return WorkerExecutionOutcome("completed", run=updated_run)

        class RecordingSessionTracker:
            def __init__(self):
                self.remembered = []

            def remember_worker_outcome(self, run, fallback_worker, outcome):
                self.remembered.append((run, fallback_worker, outcome.kind))

        core = DirectCore()
        session_tracker = RecordingSessionTracker()

        outcome = SelectedSerialWorkerExecutor(core).execute_next(
            run,
            "worker",
            client,
            CAPABILITIES,
            session_tracker=session_tracker,
            execution_policy=EXECUTION_POLICY_FAIL_FAST,
        )

        self.assertEqual(worker_output_field(outcome.run["workers"]["worker"], "status"), "done")
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
        self.assertEqual(worker_output_field(session_tracker.remembered[0][1], "status"), "done")
        self.assertEqual(session_tracker.remembered[0][2], "completed")

    def test_next_eligible_worker_executor_returns_retry_scheduled_without_retrying_inline(self):
        run = {
            "workers": {
                "worker": normalize_worker(
                    {
                        "id": "worker",
                        "prompt": "Finish the worker task",
                        "retry_limit": 1,
                        "retryable_failures": ["provider"],
                    },
                    "worker",
                )
            }
        }
        client = object()
        test_case = self

        class RetryCore:
            def __init__(self):
                self.calls = 0

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
            ):
                self.calls += 1
                if self.calls > 1:
                    test_case.fail("selected serial worker execution should not retry inline")
                updated_run = deepcopy(run)
                updated_worker = updated_run["workers"][worker_field(worker, "id")]
                updated_worker.set_field("lifecycle_state", "active_retry")
                updated_worker.set_field("retry_count", 1)
                updated_worker.set_field("last_failure_category", "provider")
                updated_worker.set_field("last_failure_reason", "transient provider outage")
                return WorkerExecutionOutcome("retry_scheduled", failure_category="provider", run=updated_run)

        class RecordingSessionTracker:
            def __init__(self):
                self.remembered = []

            def remember_worker_outcome(self, run, fallback_worker, outcome):
                self.remembered.append((run, fallback_worker, outcome.kind))

        core = RetryCore()
        session_tracker = RecordingSessionTracker()

        outcome = SelectedSerialWorkerExecutor(core).execute_next(
            run,
            "worker",
            client,
            CAPABILITIES,
            session_tracker=session_tracker,
            execution_policy=EXECUTION_POLICY_FAIL_FAST,
        )

        self.assertEqual(core.calls, 1)
        worker = outcome.run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "active")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")
        self.assertEqual(worker_field(worker, "retry_count"), 1)
        self.assertIsNone(outcome.first_error_outcome)
        self.assertIsNone(outcome.fail_fast_outcome)
        self.assertEqual(session_tracker.remembered[0][2], "retry_scheduled")

    def test_start_replans_from_persisted_retry_state_before_retry_attempt(self):
        class RecordingPlanner:
            def __init__(self):
                self.delegate = DependencyOrderedSerialPlanner()
                self.snapshots = []

            def plan(self, workers):
                worker = workers["worker"]
                self.snapshots.append(
                    (
                        worker_output_field(worker, "status"),
                        worker_output_field(worker, "next_eligible_action"),
                        worker_field(worker, "retry_count"),
                    )
                )
                return self.delegate.plan(workers)

        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_initial"]) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                retry_limit=1,
                retryable_failures=["provider"],
            )
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append(session_id)
                if len(executions) == 1:
                    raise BlockingProviderFailure("transient provider outage", prompt_id="msg_user_failed")
                return {"status": "done", "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant"}}

            planner = RecordingPlanner()
            service = scenario.service(executor=execute_prompt)
            service.scheduler.planner = planner
            outcome = service.start(scenario.request("worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, ["ses_initial", "ses_initial"])
        self.assertEqual(planner.snapshots[0], ("queued", "start", 0))
        self.assertIn(("active", "retry", 1), planner.snapshots)
        self.assertEqual(planner.snapshots[-1], ("done", "collect", 1))
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "done")
        self.assertEqual(worker_field(worker, "retry_count"), 1)

    def test_start_persists_active_attempt_before_provider_call(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_initial"]) as scenario:
            scenario.add_worker("worker", prompt="Finish the worker task")
            observed_before_call = {}

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                persisted_worker = scenario.store.load_run(RUN_NAME)["workers"]["worker"]
                observed_before_call["worker"] = deepcopy(persisted_worker)

                self.assertEqual(worker_field(persisted_worker, "session_id"), "ses_initial")
                self.assertEqual(worker_output_field(persisted_worker, "status"), "active")
                self.assertEqual(worker_output_field(persisted_worker, "next_eligible_action"), "wait")
                attempt = assert_single_worker_attempt(
                    self,
                    persisted_worker,
                    status="active",
                    session_id="ses_initial",
                )
                self.assertEqual(attempt.get("id"), "attempt-1")
                self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
                self.assertEqual(attempt.get("started_at"), NOW)
                self.assertIsNone(attempt.get("finished_at"))
                self.assertNotIn("result_status", attempt)
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            outcome = scenario.service(executor=execute_prompt).start(scenario.request("worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("worker", observed_before_call)
        self.assertEqual(
            scenario.client.requests,
            [("create", scenario.directory, None, None), ("execute", "ses_initial", "Finish the worker task")],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "done")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "collect")
        attempt = assert_single_worker_attempt(self, worker, status="completed", session_id="ses_initial")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
        self.assertEqual(attempt.get("started_at"), NOW)
        self.assertEqual(attempt.get("finished_at"), NOW)
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")

    def test_start_persists_worker_session_creation_intent_before_remote_create(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                agent="build",
                model="openai/gpt-5.5",
            )
            observed_intent = {}
            test_case = self

            class InspectingCreateClient(FakeClient):
                def create_session_response(self, directory, *, agent=None, model=None, metadata=None):
                    persisted_run = scenario.store.load_run(RUN_NAME)
                    journal = persisted_run[WORKER_SESSION_JOURNAL_FIELD]
                    test_case.assertEqual(len(journal), 1)
                    observed_intent["entry"] = deepcopy(journal[0])
                    observed_intent["metadata"] = metadata
                    return super().create_session_response(directory, agent=agent, model=model, metadata=metadata)

            scenario.client = InspectingCreateClient(["ses_initial"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            outcome = scenario.service(executor=execute_prompt).start(scenario.request("worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            scenario.client.requests,
            [
                ("create", scenario.directory, "build", "openai/gpt-5.5"),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        entry = observed_intent["entry"]
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["status"], "intent")
        self.assertEqual(entry["worker_id"], "worker")
        self.assertEqual(entry["directory"], scenario.directory)
        self.assertEqual(entry["agent"], "build")
        self.assertEqual(entry["model"], "openai/gpt-5.5")
        self.assertFalse(entry["cleanup_requested"])
        self.assertNotIn("session_id", entry)
        self.assertEqual(
            observed_intent["metadata"],
            {
                "ocs.remote_mutation_kind": "worker_session_create",
                "ocs.remote_mutation_id": entry["id"],
                "ocs.worker_id": "worker",
                "ocs.cleanup_requested": "false",
                "ocs.run_name": RUN_NAME,
            },
        )
        self.assertNotIn(WORKER_SESSION_JOURNAL_FIELD, run)

    def test_start_persistence_failure_after_session_creation_leaves_cleanup_metadata(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = RunStore(store_root)
            inner_store.create_run(RUN_NAME, directory=directory, server_url=SERVER_URL)
            inner_store.upsert_worker(RUN_NAME, "worker", role="worker", prompt="Finish the worker task")
            store = FailAfterCreatedSessionJournalStore(inner_store)
            client = FakeClient(["ses_initial"])
            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("worker should not execute after persistence failure"),
                now=lambda: NOW,
            )

            with self.assertRaisesRegex(RunStoreError, "forced update failure after session creation"):
                service.start(
                    DependencyOrderedSerialRunStartRequest(
                        name=RUN_NAME,
                        worker_id="worker",
                        role="worker",
                        cleanup=True,
                    )
                )
            run = inner_store.load_run(RUN_NAME)

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
        self.assertEqual(worker_field(worker, "session_id"), "ses_initial")
        self.assertEqual(
            worker_field(worker, "cleanup"),
            {"requested": True, "deleted": False, "sessions": ["ses_initial"]},
        )

    def test_command_service_start_passes_injected_dependencies_to_orchestration(self):
        with DependencyOrderedSerialServiceScenario(self, client=FakeClient([])) as scenario:
            scenario.add_worker("worker", prompt="Finish the worker task")
            detected_clients = []

            def detect_capabilities(detected_client):
                detected_clients.append(detected_client)
                return UNSUPPORTED_CAPABILITIES

            service = RunCommandService(
                scenario.store,
                client_factory=lambda url: scenario.client,
                capability_detector=detect_capabilities,
                now=lambda: NOW,
            )

            outcome = service.start_run(RunStartRequest(name=RUN_NAME, worker_id="worker", role="worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 70)
        self.assertEqual(detected_clients, [scenario.client])
        self.assertEqual(run["updated_at"], NOW)

    def test_start_unsupported_blocking_execution_is_not_retryable(self):
        with DependencyOrderedSerialServiceScenario(self, capabilities=UNSUPPORTED_CAPABILITIES) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                retry_limit=1,
                retryable_failures=["api"],
            )
            outcome = scenario.start("worker")
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 70)
        self.assertIn("unsupported route behavior", outcome.error)
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(worker, "status"), "failed")
        self.assertEqual(worker_field(worker, "failure_category"), "api")
        self.assertEqual(worker_field(worker, "retryable_failures"), ["api"])
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "none")

    def test_start_api_setup_failure_is_not_retryable(self):
        def detect_capabilities(client):
            raise OpenCodeApiError("capability probe failed")

        with DependencyOrderedSerialServiceScenario(self, capability_detector=detect_capabilities) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                retry_limit=1,
                retryable_failures=["api"],
            )
            outcome = scenario.start("worker")
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "api failure: capability probe failed")
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(worker, "status"), "failed")
        self.assertEqual(worker_field(worker, "failure_category"), "api")
        self.assertEqual(worker_field(worker, "retryable_failures"), ["api"])
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "none")


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
