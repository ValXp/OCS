from dataclasses import dataclass
import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_multi_worker_server, payloads_for, request_paths

from opencode_session.run_store import RunStore
from opencode_session.worker_state import apply_worker_transition_to_worker, mark_worker_failed


@dataclass
class CliScenarioResult:
    start: object
    payload: dict
    requests: list
    directory: str


class DependencyOrderedSerialOrchestrationCliTest(unittest.TestCase):
    def run_dependency_ordered_serial_scenario(self, workers, *, server_config=None, start_args=()):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(server, **(server_config or {}))
                self.assert_cli_success(
                    run_ocs(
                        "run",
                        "--store",
                        store,
                        "init",
                        "demo",
                        "--directory",
                        directory,
                        "--server",
                        server.url,
                    ),
                    "init",
                )
                for worker in workers:
                    self.assert_cli_success(run_ocs(*self.worker_command(store, worker)), f"worker {worker['id']}")
                    if worker.get("fail_before_start"):
                        self.mark_worker_failed(store, worker["id"])
                start = run_ocs("run", "--store", store, "start", "demo", *start_args)
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")
            self.assert_cli_success(status, "status")
            payload = load_json(self, status, "status")

        return CliScenarioResult(start=start, payload=payload, requests=requests, directory=directory)

    def mark_worker_failed(self, store, worker_id):
        def fail_worker(run):
            worker = run["workers"][worker_id]
            apply_worker_transition_to_worker(
                worker,
                mark_worker_failed(worker, "provider", "preexisting failure", retryable=False),
            )

        RunStore(store).update_run("demo", fail_worker)

    def worker_command(self, store, worker):
        args = ["run", "--store", store, "worker", "demo", worker["id"], "--role", worker["role"]]
        if "prompt" in worker and worker["prompt"] is not None:
            args.extend(["--prompt", worker["prompt"]])
        for key, flag in (("agent", "--agent"), ("model", "--model"), ("session", "--session"), ("status", "--status")):
            if worker.get(key) is not None:
                args.extend([flag, worker[key]])
        dependencies = worker.get("depends_on", ())
        if isinstance(dependencies, str):
            dependencies = (dependencies,)
        for dependency in dependencies:
            args.extend(["--depends-on", dependency])
        return args

    def assert_cli_success(self, result, description):
        self.assertEqual(result.returncode, 0, f"{description} failed\n{format_completed_process(result)}")

    def test_start_executes_independent_ready_workers_as_serial_steps_through_blocking_executor(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {
                    "id": "planner",
                    "role": "plan",
                    "prompt": "Create the implementation plan",
                    "agent": "plan",
                    "model": "openai/gpt-5.5",
                },
                {
                    "id": "docs",
                    "role": "write",
                    "prompt": "Draft the release notes",
                    "agent": "build",
                    "model": "openai/gpt-5.5-mini",
                },
            ]
        )

        self.assertEqual(scenario.start.returncode, 0, format_completed_process(scenario.start))
        self.assertEqual(
            request_paths(scenario.requests)[2:],
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
            payloads_for(scenario.requests, "POST", "/api/session"),
            [
                {"location": {"directory": scenario.directory}, "agent": "build", "model": "openai/gpt-5.5-mini"},
                {"location": {"directory": scenario.directory}, "agent": "plan", "model": "openai/gpt-5.5"},
            ],
        )
        self.assertIn("run=demo status=done", scenario.start.stdout)
        payload = scenario.payload
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
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "planner", "role": "plan", "prompt": "Create the implementation plan"},
                {"id": "docs", "role": "write", "prompt": "Draft the release notes"},
            ],
            start_args=("--cleanup",),
        )

        self.assertEqual(scenario.start.returncode, 0, format_completed_process(scenario.start))
        payload = scenario.payload
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(payload["workers"]["planner"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(payloads_for(scenario.requests, "DELETE", "/api/session/ses_docs"), [None])
        self.assertEqual(payloads_for(scenario.requests, "DELETE", "/api/session/ses_plan"), [None])

    def test_start_with_cleanup_does_not_delete_preexisting_worker_sessions(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {
                    "id": "metadata",
                    "role": "build",
                    "prompt": "Use the stored session",
                    "session": "ses_metadata",
                },
                {"id": "argument", "role": "review", "prompt": "Use the session passed to start"},
            ],
            server_config={
                "session_ids": ["ses_unused"],
                "run_payloads": {
                    "ses_metadata": {"id": "msg_metadata_user", "status": "submitted"},
                    "ses_argument": {"id": "msg_argument_user", "status": "submitted"},
                },
                "reply_payloads": {
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
            },
            start_args=("--worker", "argument", "--session", "ses_argument", "--cleanup"),
        )

        self.assertEqual(scenario.start.returncode, 0, format_completed_process(scenario.start))
        payload = scenario.payload
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["workers"]["metadata"]["session_id"], "ses_metadata")
        self.assertEqual(payload["workers"]["metadata"]["status"], "done")
        self.assertEqual(payload["workers"]["argument"]["session_id"], "ses_argument")
        self.assertEqual(payload["workers"]["argument"]["status"], "done")
        self.assertEqual(payloads_for(scenario.requests, "POST", "/api/session"), [])
        self.assertFalse(any(method == "DELETE" for method, _path, _payload in scenario.requests))

    def test_start_blocks_dependent_worker_when_prerequisite_fails(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "build", "role": "build", "prompt": "Run the implementation"},
                {
                    "id": "review",
                    "role": "review",
                    "prompt": "Review the implementation",
                    "depends_on": ["build"],
                },
            ],
            server_config={
                "session_ids": ["ses_build"],
                "run_payloads": {"ses_build": {"id": "msg_build_user", "status": "failed", "error": "tests failed"}},
                "reply_payloads": {},
            },
        )

        self.assertEqual(scenario.start.returncode, 69)
        self.assertIn("provider failure", scenario.start.stderr)
        self.assertEqual(
            [path for path in request_paths(scenario.requests) if path[0] == "POST"],
            [
                ("POST", "/api/session"),
                ("POST", "/session/ses_build/run"),
            ],
        )
        self.assertEqual(payloads_for(scenario.requests, "POST", "/api/session"), [{"location": {"directory": scenario.directory}}])
        self.assertEqual(
            payloads_for(scenario.requests, "POST", "/session/ses_build/run"),
            [{"message": "Run the implementation"}],
        )
        payload = scenario.payload
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], [])
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["error"], "tests failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["session_id"], None)
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])

    def test_start_persists_dependency_blocking_when_prerequisite_is_already_failed(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "build", "role": "build", "prompt": "Run the implementation", "fail_before_start": True},
                {
                    "id": "review",
                    "role": "review",
                    "prompt": "Review the implementation",
                    "depends_on": ["build"],
                },
            ]
        )

        self.assertEqual(scenario.start.returncode, 69)
        self.assertIn("run=demo status=failed", scenario.start.stdout)
        self.assertFalse(any(method == "POST" for method, _path, _payload in scenario.requests))
        payload = scenario.payload
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])
        self.assertEqual(payload["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_blocks_workers_in_dependency_cycle(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "a", "role": "build", "prompt": "Run worker A", "depends_on": ["b"]},
                {"id": "b", "role": "review", "prompt": "Run worker B", "depends_on": ["a"]},
            ]
        )

        self.assertEqual(scenario.start.returncode, 75)
        self.assertIn("run=demo status=blocked", scenario.start.stdout)
        self.assertFalse(any(path[0] == "POST" for path in request_paths(scenario.requests)))
        payload = scenario.payload
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["workers"]["a"]["status"], "blocked")
        self.assertEqual(payload["workers"]["a"]["blockers"], ["dependency-cycle:a->b->a"])
        self.assertEqual(payload["workers"]["a"]["next_eligible_action"], "resolve_blocker")
        self.assertEqual(payload["workers"]["b"]["status"], "blocked")
        self.assertEqual(payload["workers"]["b"]["blockers"], ["dependency-cycle:a->b->a"])
        self.assertEqual(payload["workers"]["b"]["next_eligible_action"], "resolve_blocker")

    def test_start_blocks_prompted_worker_waiting_on_unprompted_worker(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "setup", "role": "build"},
                {
                    "id": "review",
                    "role": "review",
                    "prompt": "Review the implementation",
                    "depends_on": ["setup"],
                },
            ]
        )

        self.assertEqual(scenario.start.returncode, 75)
        self.assertIn("run=demo status=blocked", scenario.start.stdout)
        self.assertFalse(any(path[0] == "POST" for path in request_paths(scenario.requests)))
        payload = scenario.payload
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["workers"]["setup"]["status"], "queued")
        self.assertEqual(payload["workers"]["setup"].get("prompt"), None)
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency-not-runnable:setup"])
        self.assertEqual(payload["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_returns_partial_failure_exit_code_when_some_workers_complete_before_failure(self):
        scenario = self.run_dependency_ordered_serial_scenario(
            [
                {"id": "docs", "role": "write", "prompt": "Draft the release notes"},
                {"id": "planner", "role": "plan", "prompt": "Create the implementation plan"},
            ],
            server_config={
                "session_ids": ["ses_docs", "ses_plan"],
                "run_payloads": {
                    "ses_docs": {"id": "msg_docs_user", "status": "submitted"},
                    "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
                },
                "reply_payloads": {
                    "ses_docs": {
                        "id": "msg_docs_assistant",
                        "status": "completed",
                        "cost": 0.02,
                        "tokens": {"total": 17},
                        "text": "Docs ready.",
                    },
                    "ses_plan": {"id": "msg_plan_assistant", "status": "failed", "error": "planner failed"},
                },
            },
        )

        self.assertEqual(scenario.start.returncode, 1)
        self.assertIn("planner failed", scenario.start.stderr)
        payload = scenario.payload
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], ["docs:msg_docs_assistant"])
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["status"], "failed")
        self.assertEqual(payload["workers"]["planner"]["failure_category"], "provider")
        self.assertEqual(payload["workers"]["planner"]["failure_reason"], "planner failed")


if __name__ == "__main__":
    unittest.main()
