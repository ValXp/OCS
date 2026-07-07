import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.worker_state import worker_field, worker_has_field, worker_output_field

try:
    from tests.multi_worker_orchestration_helpers import (
        CAPABILITIES,
        DependencyOrderedSerialServiceScenario,
        RUN_NAME,
        assert_blocked_worker,
    )
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import (
        CAPABILITIES,
        DependencyOrderedSerialServiceScenario,
        RUN_NAME,
        assert_blocked_worker,
    )


class DependencyOrderedSerialOrchestrationServiceDependencyTest(unittest.TestCase):
    def test_start_keeps_dependency_blocker_when_capability_probe_fails(self):
        cases = [
            {
                "name": "failed_dependency",
                "workers": [
                    ("build", {"role": "build", "prompt": "Run the implementation", "status": "failed"}),
                    ("review", {"role": "review", "prompt": "Review the implementation", "dependencies": ["build"]}),
                    ("docs", {"role": "write", "prompt": "Draft the release notes"}),
                ],
                "status_assertions": {"build": "failed", "docs": "failed"},
            },
            {
                "name": "missing_dependency",
                "workers": [
                    ("review", {"role": "review", "prompt": "Review the implementation", "dependencies": ["build"]}),
                    ("docs", {"role": "write", "prompt": "Draft the release notes"}),
                ],
                "status_assertions": {"docs": "failed"},
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                detector_calls = []

                def detect_capabilities(client):
                    detector_calls.append(client)
                    raise OpenCodeApiError("capability probe failed")

                with DependencyOrderedSerialServiceScenario(self, capability_detector=detect_capabilities) as scenario:
                    for worker_id, worker_changes in case["workers"]:
                        scenario.add_worker(worker_id, **worker_changes)

                    outcome = scenario.start("review", role="review")
                    run = scenario.load_run()

                self.assertEqual(outcome.exit_code, 69)
                self.assertEqual(outcome.error, "api failure: capability probe failed")
                self.assertEqual(detector_calls, [scenario.client])
                self.assertEqual(scenario.client.requests, [])
                self.assertEqual(run["status"], "failed")
                for worker_id, status in case["status_assertions"].items():
                    self.assertEqual(worker_output_field(run["workers"][worker_id], "status"), status)
                self.assertEqual(worker_field(run["workers"]["docs"], "failure_category"), "api")
                assert_blocked_worker(self, run, "review", ["dependency:build"])
                self.assertIsNone(worker_field(run["workers"]["review"], "failure_category"))
                self.assertIsNone(worker_field(run["workers"]["review"], "error"))

    def test_start_blocks_failed_and_missing_dependency_chains_before_probe(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker("build", role="build", prompt="Run the implementation", status="failed")
            scenario.add_worker("review", role="review", prompt="Review the implementation", dependencies=["build"])
            scenario.add_worker("deploy", role="deploy", prompt="Deploy the reviewed implementation", dependencies=["review"])
            scenario.add_worker("docs", role="write", prompt="Draft the docs", dependencies=["missing"])
            scenario.add_worker("publish", role="publish", prompt="Publish the docs", dependencies=["docs"])
            detector_calls = []

            def detect_capabilities(client):
                detector_calls.append(client)
                return CAPABILITIES

            outcome = scenario.service(capability_detector=detect_capabilities).start(scenario.request("deploy"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 69)
        self.assertIsNone(outcome.error)
        self.assertEqual(detector_calls, [])
        self.assertEqual(scenario.client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(run["workers"]["build"], "status"), "failed")
        assert_blocked_worker(self, run, "review", ["dependency:build"])
        assert_blocked_worker(self, run, "deploy", ["dependency:review"])
        assert_blocked_worker(self, run, "docs", ["dependency:missing"])
        assert_blocked_worker(self, run, "publish", ["dependency:docs"])

    def test_start_blocks_only_failed_dependency_when_another_dependency_is_done(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "docs",
                role="write",
                prompt="Draft the release notes",
                status="done",
                output_refs=["assistant:msg_docs_assistant"],
            )
            scenario.add_worker(
                "build",
                role="build",
                prompt="Run the implementation",
                status="failed",
            )
            scenario.add_worker(
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["docs", "build"],
            )
            outcome = scenario.start("review", role="review")
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(scenario.client.requests, [])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["output_refs"], ["docs:msg_docs_assistant"])
        self.assertEqual(worker_output_field(run["workers"]["docs"], "status"), "done")
        self.assertEqual(worker_output_field(run["workers"]["build"], "status"), "failed")
        assert_blocked_worker(self, run, "review", ["dependency:build"])

    def test_continue_policy_runs_next_independent_ready_worker_serially_after_failure(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_alpha", "ses_beta"]) as scenario:
            scenario.add_worker("alpha", role="build", prompt="Run alpha")
            scenario.add_worker("beta", role="write", prompt="Run beta")

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                if session_id == "ses_alpha":
                    raise BlockingProviderFailure("alpha failed", prompt_id="msg_alpha_user")
                return {
                    "message_ids": {"user": "msg_beta_user", "assistant": "msg_beta_assistant"},
                    "status": "done",
                }

            outcome = scenario.service(executor=execute_prompt).start(
                scenario.request("alpha", role="build", execution_policy="continue")
            )
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.error, "provider failure: alpha failed")
        self.assertEqual(
            scenario.client.requests,
            [
                ("create", scenario.directory, None, None),
                ("execute", "ses_alpha", "Run alpha"),
                ("create", scenario.directory, None, None),
                ("execute", "ses_beta", "Run beta"),
            ],
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(run["workers"]["alpha"], "status"), "failed")
        self.assertEqual(worker_output_field(run["workers"]["beta"], "status"), "done")
        self.assertEqual(worker_field(run["workers"]["beta"], "output_refs"), ["assistant:msg_beta_assistant"])

    def test_fail_fast_policy_stops_before_next_independent_ready_worker(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_alpha"]) as scenario:
            scenario.add_worker("alpha", role="build", prompt="Run alpha")
            scenario.add_worker("beta", role="write", prompt="Run beta")

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                if session_id != "ses_alpha":
                    self.fail("fail-fast should not execute the next independent worker")
                raise BlockingProviderFailure("alpha failed", prompt_id="msg_alpha_user")

            outcome = scenario.service(executor=execute_prompt).start(scenario.request("alpha", role="build"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "provider failure: alpha failed")
        self.assertEqual(
            scenario.client.requests,
            [
                ("create", scenario.directory, None, None),
                ("execute", "ses_alpha", "Run alpha"),
            ],
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(run["workers"]["alpha"], "status"), "failed")
        self.assertEqual(worker_output_field(run["workers"]["beta"], "status"), "queued")
        self.assertIsNone(worker_field(run["workers"]["beta"], "session_id"))

    def test_start_does_not_probe_capabilities_when_partially_completed_cycle_blocks_worker(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "build",
                role="build",
                prompt="Run the implementation",
                status="done",
                dependencies=["review"],
                output_refs=["assistant:msg_build_assistant"],
            )
            scenario.add_worker(
                "review",
                role="review",
                prompt="Review the implementation",
                dependencies=["build"],
            )
            detector_calls = []
            executions = []

            def detect_capabilities(client):
                detector_calls.append(client)
                return CAPABILITIES

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                self.fail("locally blocked run should not execute workers")

            outcome = scenario.service(capability_detector=detect_capabilities, executor=execute_prompt).start(
                scenario.request("review", role="review")
            )
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 75)
        self.assertEqual(detector_calls, [])
        self.assertEqual(scenario.client.requests, [])
        self.assertEqual(executions, [])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(worker_output_field(run["workers"]["build"], "status"), "done")
        assert_blocked_worker(self, run, "review", ["dependency-cycle:build->review->build"])

    def test_start_does_not_execute_blocked_worker_after_dependency_succeeds(self):
        with DependencyOrderedSerialServiceScenario(self, session_ids=["ses_build"]) as scenario:
            scenario.add_worker("build", role="build", prompt="Run the implementation")
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

            service = scenario.service(executor=execute_prompt)
            first_outcome = service.start(scenario.request("build", role="build"))
            requests_after_first_start = list(scenario.client.requests)
            executions_after_first_start = list(executions)
            scenario.add_worker(
                "review",
                role="review",
                prompt="Review the implementation",
                session_id="ses_review",
                dependencies=["build"],
                status="blocked",
                blockers=["manual:blocker"],
            )

            second_outcome = service.start(scenario.request("review", role="review"))
            run = scenario.load_run()

        self.assertEqual(first_outcome.exit_code, 0)
        self.assertEqual(second_outcome.exit_code, 75)
        self.assertEqual(requests_after_first_start, [("create", scenario.directory, None, None)])
        self.assertEqual(executions_after_first_start, [("ses_build", "Run the implementation")])
        self.assertEqual(scenario.client.requests, requests_after_first_start)
        self.assertEqual(executions, executions_after_first_start)
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["output_refs"], ["build:msg_build_assistant"])
        self.assertEqual(worker_output_field(run["workers"]["build"], "status"), "done")
        assert_blocked_worker(self, run, "review", ["manual:blocker"])

    def test_start_requeued_worker_finishes_without_stale_status_metadata(self):
        with DependencyOrderedSerialServiceScenario(self) as scenario:
            scenario.add_worker(
                "build",
                role="build",
                prompt="Run the implementation",
                status="done",
                output_refs=["assistant:msg_build_assistant"],
            )
            scenario.add_worker(
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
                worker.update_canonical_fields(
                    error="previous failure",
                    failure_category="api",
                    failure_reason="previous failure",
                    failure_retryable=False,
                    last_failure_category="api",
                    last_failure_reason="previous failure",
                )

            scenario.store.update_run(RUN_NAME, seed_stale_metadata)
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_review_user", "assistant": "msg_review_assistant"},
                    "status": "done",
                }

            outcome = scenario.service(executor=execute_prompt).start(scenario.request("review", role="review"))
            run = scenario.load_run()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(executions, [("ses_review", "Review the implementation")])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["output_refs"], ["build:msg_build_assistant", "review:msg_review_assistant"])
        review = run["workers"]["review"]
        self.assertEqual(worker_output_field(review, "status"), "done")
        self.assertEqual(worker_field(review, "blockers"), [])
        self.assertFalse(worker_has_field(review, "error"))
        self.assertIsNone(worker_field(review, "failure_category"))
        self.assertIsNone(worker_field(review, "failure_reason"))
        self.assertFalse(worker_has_field(review, "failure_retryable"))
        self.assertEqual(worker_field(review, "last_failure_category"), "api")
        self.assertEqual(worker_field(review, "last_failure_reason"), "previous failure")
        self.assertEqual(worker_output_field(review, "next_eligible_action"), "collect")


if __name__ == "__main__":
    unittest.main()
