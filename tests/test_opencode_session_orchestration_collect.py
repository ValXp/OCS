import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, run_ocs
    from tests.orchestration_cli_harness import configure_multi_worker_server, configure_single_worker_server, request_paths
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, run_ocs
    from orchestration_cli_harness import configure_multi_worker_server, configure_single_worker_server, request_paths


class CollectOrchestrationCliTest(unittest.TestCase):
    def test_collect_prints_completed_worker_outputs_in_dependency_order(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_multi_worker_server(
                    server,
                    session_ids=["ses_plan", "ses_review"],
                    run_payloads={
                        "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
                        "ses_review": {"id": "msg_review_user", "status": "submitted"},
                    },
                    reply_payloads={
                        "ses_plan": {
                            "id": "msg_plan_assistant",
                            "status": "completed",
                            "cost": 0.01,
                            "tokens": {"total": 12},
                            "text": "Plan ready.",
                        },
                        "ses_review": {
                            "id": "msg_review_assistant",
                            "status": "completed",
                            "cost": 0.03,
                            "tokens": {"total": 15},
                            "text": "Review done.",
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
                plan = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "plan",
                    "--role",
                    "plan",
                    "--prompt",
                    "Plan the work",
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
                    "Review the plan",
                    "--depends-on",
                    "plan",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            collect = run_ocs("run", "--store", store, "collect", "demo")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(plan.returncode, 0, format_completed_process(plan))
        self.assertEqual(review.returncode, 0, format_completed_process(review))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(collect.returncode, 0, format_completed_process(collect))
        self.assertEqual(collect.stderr, "")
        self.assertEqual(
            request_paths(requests)[2:],
            [
                ("POST", "/api/session"),
                ("POST", "/session/ses_plan/run"),
                ("POST", "/session/ses_plan/reply"),
                ("POST", "/api/session"),
                ("POST", "/session/ses_review/run"),
                ("POST", "/session/ses_review/reply"),
            ],
        )
        self.assertEqual(
            collect.stdout,
            "worker=plan role=plan session=ses_plan status=done user=msg_plan_user "
            "assistant=msg_plan_assistant cost=0.01 tokens=12 text=\"Plan ready.\"\n"
            "worker=review role=review session=ses_review status=done user=msg_review_user "
            "assistant=msg_review_assistant cost=0.03 tokens=15 text=\"Review done.\"\n",
        )

    def test_collect_returns_stored_compact_worker_result_without_server(self):
        events = [{"type": "session.status", "properties": {"sessionID": "ses_new", "status": "completed"}}]

        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server, events=events)
                start = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
            collect = run_ocs("run", "--store", store, "collect", "demo")

        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(collect.returncode, 0, format_completed_process(collect))
        self.assertEqual(collect.stderr, "")
        self.assertEqual(
            collect.stdout,
            "run_blocking session=ses_new status=done user=msg_user_1 assistant=msg_assistant_1 "
            "cost=0.015 tokens=20 text=\"Worker finished.\"\n",
        )


if __name__ == "__main__":
    unittest.main()
