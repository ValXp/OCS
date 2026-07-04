import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_worker_control_server, payloads_for
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_worker_control_server, payloads_for


class ControlOrchestrationCliTest(unittest.TestCase):
    def test_run_steer_targets_individual_worker_session_and_records_prompt(self):
        prompt_response = {
            "sessionID": "ses_plan",
            "messageID": "msg_steer_1",
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 4,
        }
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_worker_control_server(server, prompt_response=prompt_response)
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
                worker = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                steer = run_ocs(
                    "run",
                    "--store",
                    store,
                    "steer",
                    "demo",
                    "planner",
                    "Incorporate the review feedback",
                    "--message-id",
                    "msg_steer_1",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(steer.returncode, 0, format_completed_process(steer))
        self.assertEqual(steer.stderr, "")
        self.assertEqual(
            steer.stdout,
            "run=demo worker=planner steer session=ses_plan message=msg_steer_1 "
            "delivery=steer status=queued admitted=4 promoted=-\n",
        )
        self.assertEqual(
            payloads_for(requests, "POST", "/api/session/ses_plan/prompt"),
            [
                {
                    "id": "msg_steer_1",
                    "prompt": {"text": "Incorporate the review feedback"},
                    "delivery": "steer",
                }
            ],
        )
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["workers"]["planner"]["status"], "active")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_steer_1"])

    def test_run_steer_treats_matching_replay_as_admitted_prompt(self):
        prompt_response = {
            "sessionID": "ses_plan",
            "messageID": "msg_repeat_1",
            "delivery": "queue",
            "state": "admitted",
            "admittedSequence": 7,
            "duplicate": True,
        }
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_worker_control_server(server, prompt_response=prompt_response, prompt_status=409)
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
                worker = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                steer = run_ocs(
                    "run",
                    "--store",
                    store,
                    "steer",
                    "demo",
                    "planner",
                    "Queue this without duplicating it",
                    "--delivery",
                    "queue",
                    "--message-id",
                    "msg_repeat_1",
                )
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(steer.returncode, 0, format_completed_process(steer))
        self.assertEqual(steer.stderr, "")
        self.assertEqual(
            steer.stdout,
            "run=demo worker=planner steer session=ses_plan message=msg_repeat_1 "
            "delivery=queue status=queued admitted=7 promoted=-\n",
        )
        payload = load_json(self, status, "status")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_repeat_1"])

    def test_run_abort_targets_individual_worker_session_and_marks_worker_aborted(self):
        abort_response = {"sessionID": "ses_plan", "accepted": True, "status": "aborted"}
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_worker_control_server(server, abort_response=abort_response)
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
                worker = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                abort = run_ocs("run", "--store", store, "abort", "demo", "planner")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(abort.returncode, 0, format_completed_process(abort))
        self.assertEqual(abort.stderr, "")
        self.assertEqual(abort.stdout, "run=demo worker=planner abort session=ses_plan accepted=true status=aborted\n")
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_plan/abort"), [{}])
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "aborted")
        self.assertEqual(payload["workers"]["planner"]["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
