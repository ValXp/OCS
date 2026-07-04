import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths


class MultiWorkerOrchestrationCliTest(unittest.TestCase):
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
                ("POST", "/api/session"),
                ("POST", "/session/ses_docs/run"),
                ("POST", "/session/ses_docs/reply"),
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


if __name__ == "__main__":
    unittest.main()
