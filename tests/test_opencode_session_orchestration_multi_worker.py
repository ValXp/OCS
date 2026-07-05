import tempfile
import threading
import unittest
from unittest import mock

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import MultiWorkerRunOrchestrationService, MultiWorkerRunStartRequest
from opencode_session.run_start_core import RunStartCore
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths


CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": True},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": True},
    },
    "blocking_message_available": False,
    "blocking_execution_available": True,
    "legacy_fallback_available": True,
}

UNSUPPORTED_CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": False},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": False},
    },
    "blocking_message_available": False,
    "blocking_execution_available": False,
    "legacy_fallback_available": False,
}


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeClient:
    def __init__(self, session_ids, *, delete_failures=None):
        self.requests = []
        self.session_ids = list(session_ids)
        self.delete_failures = dict(delete_failures or {})

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session(self, session_id):
        self.requests.append(("delete", session_id))
        if session_id in self.delete_failures:
            raise OpenCodeApiError(self.delete_failures[session_id])


class MultiWorkerOrchestrationCliTest(unittest.TestCase):
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

    def test_start_executes_each_ready_worker_through_blocking_executor(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                planner = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--prompt",
                    "Create the implementation plan",
                    "--agent",
                    "plan",
                    "--model",
                    "openai/gpt-5.5",
                )
                docs = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "docs",
                    "--role",
                    "write",
                    "--prompt",
                    "Draft the release notes",
                    "--agent",
                    "build",
                    "--model",
                    "openai/gpt-5.5-mini",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(planner.returncode, 0, format_completed_process(planner))
        self.assertEqual(docs.returncode, 0, format_completed_process(docs))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            request_paths(requests)[2:],
            [
                ("POST", "/api/session"),
                ("POST", "/session/ses_docs/run"),
                ("POST", "/session/ses_docs/reply"),
                ("POST", "/api/session"),
                ("POST", "/session/ses_plan/run"),
                ("POST", "/session/ses_plan/reply"),
            ],
        )
        self.assertEqual(
            payloads_for(requests, "POST", "/api/session"),
            [
                {"location": {"directory": directory}, "agent": "build", "model": "openai/gpt-5.5-mini"},
                {"location": {"directory": directory}, "agent": "plan", "model": "openai/gpt-5.5"},
            ],
        )
        self.assertIn("run=demo status=done", start.stdout)
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["output_refs"], ["docs:msg_docs_assistant", "planner:msg_plan_assistant"])
        self.assertEqual(payload["workers"]["planner"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["session_id"], "ses_plan")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_plan_user"])
        self.assertEqual(payload["workers"]["planner"]["output_refs"], ["assistant:msg_plan_assistant"])
        self.assertEqual(payload["workers"]["planner"]["result"]["text"], "Plan ready.")
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["session_id"], "ses_docs")
        self.assertEqual(payload["workers"]["docs"]["prompt_ids"], ["msg_docs_user"])
        self.assertEqual(payload["workers"]["docs"]["output_refs"], ["assistant:msg_docs_assistant"])
        self.assertEqual(payload["workers"]["docs"]["result"]["text"], "Docs ready.")

    def test_start_with_cleanup_deletes_created_worker_sessions_and_records_cleanup(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                planner = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--prompt",
                    "Create the implementation plan",
                )
                docs = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "docs",
                    "--role",
                    "write",
                    "--prompt",
                    "Draft the release notes",
                )
                start = run_ocs("run", "--store", store, "start", "demo", "--cleanup")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(planner.returncode, 0, format_completed_process(planner))
        self.assertEqual(docs.returncode, 0, format_completed_process(docs))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(payload["workers"]["planner"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(payloads_for(requests, "DELETE", "/api/session/ses_docs"), [None])
        self.assertEqual(payloads_for(requests, "DELETE", "/api/session/ses_plan"), [None])

    def test_start_with_cleanup_after_first_ready_worker_failure_does_not_precreate_later_worker_session(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "alpha", role="build", prompt="Run alpha")
            store.upsert_worker("demo", "beta", role="review", prompt="Run beta")
            client = FakeClient(["ses_alpha", "ses_beta"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                raise BlockingProviderFailure("alpha failed")

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                MultiWorkerRunStartRequest(name="demo", worker_id="alpha", role="build", cleanup=True)
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "provider failure: alpha failed")
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_alpha", "Run alpha"),
                ("delete", "ses_alpha"),
            ],
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(run["workers"]["beta"]["status"], "queued")
        self.assertIsNone(run["workers"]["beta"]["session_id"])
        self.assertNotIn("cleanup", run["workers"]["beta"])

    def test_cleanup_attempts_later_worker_after_earlier_worker_delete_fails(self):
        first_error = "DELETE /api/session/ses_alpha failed: HTTP 500"
        client = FakeClient([], delete_failures={"ses_alpha": first_error})
        run = {
            "status": "done",
            "workers": {
                "alpha": {"id": "alpha", "status": "done"},
                "beta": {"id": "beta", "status": "done"},
            },
        }
        saves = []
        core = RunStartCore(
            save_run=lambda run: saves.append(run),
            refresh_run_summary=lambda run: None,
            now=lambda: "2026-07-03T00:00:00Z",
        )

        outcome = core.cleanup_created_workers(
            client,
            run,
            {"alpha": ["ses_alpha"], "beta": ["ses_beta"]},
        )

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
        self.assertEqual(client.requests, [("delete", "ses_alpha"), ("delete", "ses_beta")])
        self.assertEqual(
            run["workers"]["alpha"]["cleanup"],
            {"requested": True, "deleted": False, "error": first_error},
        )
        self.assertEqual(run["workers"]["alpha"]["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["failure_reason"], first_error)
        self.assertEqual(run["workers"]["beta"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(saves, [run])

    def test_start_with_cleanup_does_not_delete_preexisting_worker_sessions(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(
                    server,
                    session_ids=["ses_unused"],
                    run_payloads={
                        "ses_metadata": {"id": "msg_metadata_user", "status": "submitted"},
                        "ses_argument": {"id": "msg_argument_user", "status": "submitted"},
                    },
                    reply_payloads={
                        "ses_metadata": {
                            "id": "msg_metadata_assistant",
                            "status": "completed",
                            "text": "Metadata worker done.",
                        },
                        "ses_argument": {
                            "id": "msg_argument_assistant",
                            "status": "completed",
                            "text": "Argument worker done.",
                        },
                    },
                )
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                metadata = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "metadata",
                    "--role",
                    "build",
                    "--prompt",
                    "Use the stored session",
                    "--session",
                    "ses_metadata",
                )
                argument = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "argument",
                    "--role",
                    "review",
                    "--prompt",
                    "Use the session passed to start",
                )
                start = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--worker",
                    "argument",
                    "--session",
                    "ses_argument",
                    "--cleanup",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(metadata.returncode, 0, format_completed_process(metadata))
        self.assertEqual(argument.returncode, 0, format_completed_process(argument))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["workers"]["metadata"]["session_id"], "ses_metadata")
        self.assertEqual(payload["workers"]["metadata"]["status"], "done")
        self.assertEqual(payload["workers"]["argument"]["session_id"], "ses_argument")
        self.assertEqual(payload["workers"]["argument"]["status"], "done")
        self.assertEqual(payloads_for(requests, "POST", "/api/session"), [])
        self.assertFalse(any(method == "DELETE" for method, _path, _payload in requests))

    def test_start_blocks_dependent_worker_when_prerequisite_fails(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(
                    server,
                    session_ids=["ses_build"],
                    run_payloads={"ses_build": {"id": "msg_build_user", "status": "failed", "error": "tests failed"}},
                    reply_payloads={},
                )
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                build = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "build",
                    "--role",
                    "build",
                    "--prompt",
                    "Run the implementation",
                )
                review = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the implementation",
                    "--depends-on",
                    "build",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(build.returncode, 0, format_completed_process(build))
        self.assertEqual(review.returncode, 0, format_completed_process(review))
        self.assertEqual(start.returncode, 69)
        self.assertIn("provider failure", start.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            [path for path in request_paths(requests) if path[0] == "POST"],
            [
                ("POST", "/api/session"),
                ("POST", "/session/ses_build/run"),
            ],
        )
        self.assertEqual(payloads_for(requests, "POST", "/api/session"), [{"location": {"directory": directory}}])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_build/run"), [{"message": "Run the implementation"}])
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], [])
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["error"], "tests failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["session_id"], None)
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])

    def test_start_persists_dependency_blocking_when_prerequisite_is_already_failed(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                build = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "build",
                    "--role",
                    "build",
                    "--prompt",
                    "Run the implementation",
                    "--status",
                    "failed",
                )
                review = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the implementation",
                    "--depends-on",
                    "build",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(build.returncode, 0, format_completed_process(build))
        self.assertEqual(review.returncode, 0, format_completed_process(review))
        self.assertEqual(start.returncode, 69)
        self.assertIn("run=demo status=failed", start.stdout)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertFalse(any(method == "POST" for method, _path, _payload in requests))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])
        self.assertEqual(payload["workers"]["review"]["next_eligible_action"], "resolve_blocker")

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

    def test_start_blocks_workers_in_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker_a = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "a",
                    "--role",
                    "build",
                    "--prompt",
                    "Run worker A",
                    "--depends-on",
                    "b",
                )
                worker_b = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "b",
                    "--role",
                    "review",
                    "--prompt",
                    "Run worker B",
                    "--depends-on",
                    "a",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker_a.returncode, 0, format_completed_process(worker_a))
        self.assertEqual(worker_b.returncode, 0, format_completed_process(worker_b))
        self.assertEqual(start.returncode, 75)
        self.assertIn("run=demo status=blocked", start.stdout)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertFalse(any(path[0] == "POST" for path in request_paths(requests)))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["workers"]["a"]["status"], "blocked")
        self.assertEqual(payload["workers"]["a"]["blockers"], ["dependency-cycle:a->b->a"])
        self.assertEqual(payload["workers"]["a"]["next_eligible_action"], "resolve_blocker")
        self.assertEqual(payload["workers"]["b"]["status"], "blocked")
        self.assertEqual(payload["workers"]["b"]["blockers"], ["dependency-cycle:a->b->a"])
        self.assertEqual(payload["workers"]["b"]["next_eligible_action"], "resolve_blocker")

    def test_start_blocks_prompted_worker_waiting_on_unprompted_worker(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                setup = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "setup",
                    "--role",
                    "build",
                )
                review = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the implementation",
                    "--depends-on",
                    "setup",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(setup.returncode, 0, format_completed_process(setup))
        self.assertEqual(review.returncode, 0, format_completed_process(review))
        self.assertEqual(start.returncode, 75)
        self.assertIn("run=demo status=blocked", start.stdout)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertFalse(any(path[0] == "POST" for path in request_paths(requests)))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["workers"]["setup"]["status"], "queued")
        self.assertEqual(payload["workers"]["setup"].get("prompt"), None)
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency-not-runnable:setup"])
        self.assertEqual(payload["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_returns_partial_failure_exit_code_when_some_workers_complete_before_failure(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(
                    server,
                    session_ids=["ses_docs", "ses_plan"],
                    run_payloads={
                        "ses_docs": {"id": "msg_docs_user", "status": "submitted"},
                        "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
                    },
                    reply_payloads={
                        "ses_docs": {
                            "id": "msg_docs_assistant",
                            "status": "completed",
                            "cost": 0.02,
                            "tokens": {"total": 17},
                            "text": "Docs ready.",
                        },
                        "ses_plan": {"id": "msg_plan_assistant", "status": "failed", "error": "planner failed"},
                    },
                )
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                docs = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "docs",
                    "--role",
                    "write",
                    "--prompt",
                    "Draft the release notes",
                )
                planner = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--prompt",
                    "Create the implementation plan",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(docs.returncode, 0, format_completed_process(docs))
        self.assertEqual(planner.returncode, 0, format_completed_process(planner))
        self.assertEqual(start.returncode, 1)
        self.assertIn("planner failed", start.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], ["docs:msg_docs_assistant"])
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["status"], "failed")
        self.assertEqual(payload["workers"]["planner"]["failure_category"], "provider")
        self.assertEqual(payload["workers"]["planner"]["failure_reason"], "planner failed")

    def test_timeout_retry_abandoned_callback_keeps_original_session_after_worker_rebind(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            client = FakeClient(["ses_initial", "ses_retry"])
            release_abandoned_callback = threading.Event()
            abandoned_threads = []
            deadline_calls = []

            class DelayedFirstTimeoutDeadline:
                def __init__(self, timeout):
                    self.timeout = timeout

                def run(self, callback):
                    deadline_calls.append(self.timeout)
                    if len(deadline_calls) == 1:
                        thread = threading.Thread(target=lambda: (release_abandoned_callback.wait(1), callback()))
                        thread.start()
                        abandoned_threads.append(thread)
                        raise TimeoutExpired()
                    return callback()

            def result_for(session_id):
                return {
                    "session_id": session_id,
                    "message_ids": {"user": f"msg_user_{session_id}", "assistant": f"msg_assistant_{session_id}"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": f"Worker finished in {session_id}.",
                }

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return result_for(session_id)

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            try:
                with mock.patch("opencode_session.worker_execution.TimeoutDeadline", DelayedFirstTimeoutDeadline):
                    outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker"))
                run = store.load_run("demo")
            finally:
                release_abandoned_callback.set()
                for thread in abandoned_threads:
                    thread.join(1)

        self.assertFalse(any(thread.is_alive() for thread in abandoned_threads))
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["session_id"], "ses_retry")
        self.assertEqual(retry_worker["result"]["session_id"], "ses_retry")

    def test_cleanup_deletes_initial_and_timeout_retry_sessions_for_created_worker(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            client = FakeClient(["ses_initial", "ses_retry"])
            deadline_calls = []

            class FirstAttemptTimeoutDeadline:
                def __init__(self, timeout):
                    self.timeout = timeout

                def run(self, callback):
                    deadline_calls.append(self.timeout)
                    if len(deadline_calls) == 1:
                        raise TimeoutExpired()
                    return callback()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after isolated retry.",
                }

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            with mock.patch("opencode_session.worker_execution.TimeoutDeadline", FirstAttemptTimeoutDeadline):
                outcome = service.start(
                    MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
                )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
                ("delete", "ses_initial"),
                ("delete", "ses_retry"),
            ],
        )
        self.assertEqual(
            run["workers"]["worker"]["cleanup"],
            {"requested": True, "deleted": True, "sessions": ["ses_initial", "ses_retry"]},
        )

    def test_cleanup_attempts_timeout_retry_session_after_initial_delete_fails(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            first_error = "DELETE /api/session/ses_initial failed: HTTP 500"
            client = FakeClient(["ses_initial", "ses_retry"], delete_failures={"ses_initial": first_error})
            deadline_calls = []

            class FirstAttemptTimeoutDeadline:
                def __init__(self, timeout):
                    self.timeout = timeout

                def run(self, callback):
                    deadline_calls.append(self.timeout)
                    if len(deadline_calls) == 1:
                        raise TimeoutExpired()
                    return callback()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after isolated retry.",
                }

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            with mock.patch("opencode_session.worker_execution.TimeoutDeadline", FirstAttemptTimeoutDeadline):
                outcome = service.start(
                    MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
                )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
                ("delete", "ses_initial"),
                ("delete", "ses_retry"),
            ],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_reason"], first_error)
        self.assertEqual(
            worker["cleanup"],
            {"requested": True, "deleted": False, "error": first_error, "sessions": ["ses_retry"]},
        )


if __name__ == "__main__":
    unittest.main()
