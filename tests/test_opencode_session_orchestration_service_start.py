from copy import deepcopy
import tempfile
import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
)
from opencode_session.remote_journal import OUTBOX_STATE_APPLIED, OUTBOX_STATE_INTENT
from opencode_session.run_services import RunCommandService, RunStartRequest
from opencode_session.run_start_core import RunStartCapabilityProbe
from opencode_session.run_store import RunStore, RunStoreError
from opencode_session.worker_attempt_log import new_worker_attempt_record
from opencode_session.worker_session_provisioning import WORKER_SESSION_JOURNAL_FIELD
from opencode_session.worker_state import (
    WorkerTransition,
    apply_worker_transition,
    mark_worker_active,
    worker_field,
    worker_output_field,
)

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


LATER = "2026-07-03T00:00:01Z"


class DependencyOrderedSerialOrchestrationServiceStartTest(unittest.TestCase):
    def test_run_start_capability_probe_configures_client_and_reports_start_error(self):
        class ConfigurableClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.route_plan = None

            def configure_route_plan(self, route_plan):
                self.route_plan = route_plan

        client = ConfigurableClient()
        detector_calls = []

        def detect_capabilities(detected_client):
            detector_calls.append(detected_client)
            return UNSUPPORTED_CAPABILITIES

        outcome = RunStartCapabilityProbe(
            client_factory=lambda url: client,
            capability_detector=detect_capabilities,
        ).probe({"server_url": SERVER_URL})

        self.assertIs(outcome.client, client)
        self.assertEqual(detector_calls, [client])
        self.assertEqual(outcome.capabilities, UNSUPPORTED_CAPABILITIES)
        self.assertIn("unsupported route behavior", outcome.start_error)
        self.assertIsNotNone(client.route_plan)
        self.assertEqual(client.requests, [])

    def test_start_replans_from_persisted_retry_state_before_retry_attempt(self):
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

            service = scenario.service(executor=execute_prompt)
            snapshots = []
            original_plan = service._plan_serial_step

            def record_plan(workers):
                worker = workers["worker"]
                snapshots.append(
                    (
                        worker_output_field(worker, "status"),
                        worker_output_field(worker, "next_eligible_action"),
                        worker_field(worker, "retry_count"),
                    )
                )
                return original_plan(workers)

            service._plan_serial_step = record_plan
            outcome = service.start(scenario.request("worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, ["ses_initial", "ses_initial"])
        self.assertEqual(snapshots[0], ("queued", "start", 0))
        self.assertIn(("active", "retry", 1), snapshots)
        self.assertEqual(snapshots[-1], ("done", "collect", 1))
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "done")
        self.assertEqual(worker_field(worker, "prompt_ids"), ["msg_user_retry"])
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

    def test_status_reconciles_stranded_active_attempt_as_timeout(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                session_id="ses_initial",
                timeout_seconds=0.05,
            )
            _seed_stranded_active_attempt(scenario.store)
            service = RunCommandService(
                scenario.store,
                client_factory=lambda url: scenario.client,
                capability_detector=lambda client: CAPABILITIES,
                now=lambda: LATER,
            )

            run = service.load_run(RUN_NAME)

        self.assertEqual(run["status"], "timeout")
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "none")
        self.assertEqual(worker_field(worker, "failure_category"), "timeout")
        self.assertEqual(worker_field(worker, "failure_reason"), "worker timed out after 0.05s")
        self.assertEqual(worker_field(worker, "timeout_started_at"), NOW)
        self.assertEqual(worker_field(worker, "timed_out_at"), LATER)
        attempt = assert_single_worker_attempt(self, worker, status="failed", session_id="ses_initial")
        self.assertEqual(attempt.get("finished_at"), LATER)
        self.assertEqual(attempt.get("failure_category"), "timeout")
        self.assertEqual(attempt.get("error"), "worker timed out after 0.05s")

    def test_start_reconciles_stranded_active_attempt_before_planning(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                session_id="ses_initial",
                timeout_seconds=0.05,
            )
            _seed_stranded_active_attempt(scenario.store)
            service = DependencyOrderedSerialRunOrchestrationService(
                scenario.store,
                client_factory=lambda url: scenario.client,
                capability_detector=lambda client: CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("worker should not execute before recovered timeout returns"),
                now=lambda: LATER,
            )

            outcome = service.start(scenario.request("worker"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(outcome.error, "worker timed out after 0.05s")
        self.assertEqual(scenario.client.requests, [])
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_field(worker, "timed_out_at"), LATER)

    def test_start_after_status_recovery_retries_manual_timeout_worker(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "worker",
                prompt="Finish the worker task",
                session_id="ses_initial",
                timeout_seconds=0.05,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            _seed_stranded_active_attempt(scenario.store)
            status_service = RunCommandService(
                scenario.store,
                client_factory=lambda url: scenario.client,
                capability_detector=lambda client: CAPABILITIES,
                now=lambda: LATER,
            )
            status_run = status_service.load_run(RUN_NAME)
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                executions.append((session_id, prompt))
                return {"status": "done", "message_ids": {"user": "msg_retry", "assistant": "msg_done"}}

            outcome = scenario.service(executor=execute_prompt).start(scenario.request("worker"))
            run = scenario.load_run()

        recovered_worker = status_run["workers"]["worker"]
        self.assertEqual(worker_output_field(recovered_worker, "status"), "timeout")
        self.assertEqual(worker_output_field(recovered_worker, "next_eligible_action"), "retry")
        self.assertTrue(worker_field(recovered_worker, "manual_retry_required"))
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, [("ses_initial", "Finish the worker task")])
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "done")
        attempts = worker_field(worker, "attempts")
        self.assertEqual([attempt.get("status") for attempt in attempts], ["failed", "completed"])

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
        self.assertEqual(entry["outbox_state"], OUTBOX_STATE_INTENT)
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
        self.assertEqual(entry["outbox_state"], OUTBOX_STATE_APPLIED)
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
        and entry.get("outbox_state") == OUTBOX_STATE_APPLIED
        for entry in journal
    )


def _seed_stranded_active_attempt(store):
    def mutate(run):
        worker = run["workers"]["worker"]
        active_worker = apply_worker_transition(run["workers"], mark_worker_active(worker, now=lambda: NOW))
        attempt = new_worker_attempt_record(active_worker, started_at=NOW, created_session_ids=[])
        apply_worker_transition(run["workers"], WorkerTransition.attempt_started("worker", attempt))

    store.update_run(RUN_NAME, mutate)


if __name__ == "__main__":
    unittest.main()
