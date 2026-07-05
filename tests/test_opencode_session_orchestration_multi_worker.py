import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.multi_worker_orchestration import MultiWorkerRunOrchestrationService, MultiWorkerRunStartRequest
from opencode_session.run_store import RunStore
from opencode_session.worker_dependencies import analyze_worker_dependencies

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
                "status": "done",
                "dependencies": ["review"],
            },
            "review": {
                "id": "review",
                "prompt": "Review the implementation",
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
                "status": "queued",
                "dependencies": ["review"],
            },
            "review": {
                "id": "review",
                "prompt": "Review the implementation",
                "status": "queued",
                "dependencies": ["build"],
            },
            "build": {
                "id": "build",
                "prompt": "Run the implementation",
                "status": "failed",
            },
            "publish": {
                "id": "publish",
                "prompt": "Publish the docs",
                "status": "queued",
                "dependencies": ["docs"],
            },
            "docs": {
                "id": "docs",
                "prompt": "Draft the docs",
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


class MultiWorkerOrchestrationServiceTest(unittest.TestCase):
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
            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: UNSUPPORTED_CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("unsupported server should not execute worker"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("failed setup should not execute worker"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("locally blocked run should not execute workers"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="deploy", role="deploy"))
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
            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("blocked worker should not execute"),
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            first_outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="build", role="build"))
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

            second_outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="review", role="review"))
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


if __name__ == "__main__":
    unittest.main()
