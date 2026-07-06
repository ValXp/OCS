import tempfile
import unittest

from opencode_session.run_services import REMOTE_MUTATION_JOURNAL_FIELD, RunCommandService
from opencode_session.run_store import RunStore, RunStoreError


PROMPT_CAPABILITIES = {
    "route_availability": {
        "v2_prompt": {"path": "/api/session/{sessionID}/prompt", "method": "POST", "available": True},
    },
    "route_plan": {"v2_prompt": "/api/session/{sessionID}/prompt"},
    "v2_prompt_support": True,
    "legacy_fallback_available": False,
}


class FakeResponse:
    def __init__(self, data, body="{}"):
        self.data = data
        self.body = body


class RecordingRunClient:
    def __init__(self, *, on_prompt=None, on_abort=None, prompt_response=None, abort_response=None):
        self.on_prompt = on_prompt
        self.on_abort = on_abort
        self.prompt_response = prompt_response or {}
        self.abort_response = abort_response or {}
        self.requests = []
        self.route_plan = None

    def configure_route_plan(self, route_plan):
        self.route_plan = route_plan

    def admit_prompt_response(self, session_id, payload, prompt_path):
        self.requests.append(("prompt", session_id, payload, prompt_path))
        if self.on_prompt is not None:
            self.on_prompt(session_id, payload, prompt_path)
        return FakeResponse(self.prompt_response, body="{}")

    def abort_session_response(self, session_id):
        self.requests.append(("abort", session_id))
        if self.on_abort is not None:
            self.on_abort(session_id)
        return FakeResponse(self.abort_response, body="{}")


class FailingUpdateStore:
    def __init__(self, store, *, fail_on_update):
        self.store = store
        self.fail_on_update = fail_on_update
        self.update_count = 0

    def __getattr__(self, name):
        return getattr(self.store, name)

    def update_run(self, name, mutator):
        self.update_count += 1
        if self.update_count == self.fail_on_update:
            raise RunStoreError("forced update failure")
        return self.store.update_run(name, mutator)


class RunCommandServiceRemoteMutationJournalTest(unittest.TestCase):
    def test_steer_persists_recoverable_journal_before_prompt_admission(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = _active_worker_store(store_root, directory)

            def assert_journal_before_prompt(session_id, payload, prompt_path):
                run = store.load_run("demo")
                journal = run[REMOTE_MUTATION_JOURNAL_FIELD]
                self.assertEqual(len(journal), 1)
                self.assertEqual(journal[0]["kind"], "steer_prompt")
                self.assertEqual(journal[0]["worker_id"], "planner")
                self.assertEqual(journal[0]["session_id"], session_id)
                self.assertEqual(journal[0]["message_id"], "msg_steer_1")
                self.assertEqual(journal[0]["delivery"], "queue")
                self.assertEqual(journal[0]["text"], "Continue with the plan")
                self.assertEqual(payload["id"], "msg_steer_1")
                self.assertEqual(prompt_path, "/api/session/{sessionID}/prompt")

            client = RecordingRunClient(
                on_prompt=assert_journal_before_prompt,
                prompt_response={
                    "sessionID": "ses_plan",
                    "messageID": "msg_steer_1",
                    "delivery": "queue",
                    "state": "admitted",
                },
            )
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: PROMPT_CAPABILITIES,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            result = service.steer_worker(
                "demo",
                "planner",
                "Continue with the plan",
                delivery="queue",
                message_id="msg_steer_1",
            )
            run = store.load_run("demo")

        self.assertEqual(result.admission["message_id"], "msg_steer_1")
        self.assertNotIn(REMOTE_MUTATION_JOURNAL_FIELD, run)
        self.assertEqual(run["workers"]["planner"]["prompt_ids"], ["msg_steer_1"])
        self.assertEqual(len(client.requests), 1)

    def test_steer_keeps_journal_when_final_prompt_persistence_fails_after_api_success(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = _active_worker_store(store_root, directory)
            store = FailingUpdateStore(inner_store, fail_on_update=2)
            client = RecordingRunClient(
                prompt_response={
                    "sessionID": "ses_plan",
                    "messageID": "msg_steer_1",
                    "delivery": "queue",
                    "state": "admitted",
                }
            )
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: PROMPT_CAPABILITIES,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            with self.assertRaisesRegex(RunStoreError, "forced update failure"):
                service.steer_worker(
                    "demo",
                    "planner",
                    "Continue with the plan",
                    delivery="queue",
                    message_id="msg_steer_1",
                )
            run = inner_store.load_run("demo")

        self.assertEqual(client.requests[0][0], "prompt")
        self.assertEqual(run["workers"]["planner"]["prompt_ids"], [])
        self.assertEqual(len(run[REMOTE_MUTATION_JOURNAL_FIELD]), 1)
        journal = run[REMOTE_MUTATION_JOURNAL_FIELD][0]
        self.assertEqual(journal["kind"], "steer_prompt")
        self.assertEqual(journal["message_id"], "msg_steer_1")
        self.assertEqual(journal["text"], "Continue with the plan")

    def test_steer_marks_journal_when_discard_cleanup_fails_after_remote_error(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = _active_worker_store(store_root, directory)
            store = FailingUpdateStore(inner_store, fail_on_update=2)

            def reject_prompt(session_id, payload, prompt_path):
                raise RuntimeError("remote prompt rejected")

            client = RecordingRunClient(on_prompt=reject_prompt)
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: PROMPT_CAPABILITIES,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            with self.assertRaisesRegex(RuntimeError, "remote prompt rejected"):
                service.steer_worker(
                    "demo",
                    "planner",
                    "Continue with the plan",
                    delivery="queue",
                    message_id="msg_steer_1",
                )
            run = inner_store.load_run("demo")

        self.assertEqual(client.requests[0][0], "prompt")
        self.assertEqual(run["workers"]["planner"]["prompt_ids"], [])
        self.assertEqual(len(run[REMOTE_MUTATION_JOURNAL_FIELD]), 1)
        journal = run[REMOTE_MUTATION_JOURNAL_FIELD][0]
        self.assertEqual(journal["kind"], "steer_prompt")
        self.assertEqual(journal["message_id"], "msg_steer_1")
        self.assertEqual(journal["cleanup_failure"]["operation"], "discard_remote_mutation")
        self.assertEqual(journal["cleanup_failure"]["error_type"], "RunStoreError")
        self.assertEqual(journal["cleanup_failure"]["message"], "forced update failure")
        self.assertEqual(journal["cleanup_failure"]["recorded_at"], "2026-07-05T00:00:00Z")

    def test_abort_persists_recoverable_journal_before_remote_abort(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = _active_worker_store(store_root, directory)

            def assert_journal_before_abort(session_id):
                run = store.load_run("demo")
                journal = run[REMOTE_MUTATION_JOURNAL_FIELD]
                self.assertEqual(len(journal), 1)
                self.assertEqual(journal[0]["kind"], "abort_worker")
                self.assertEqual(journal[0]["worker_id"], "planner")
                self.assertEqual(journal[0]["session_id"], session_id)

            client = RecordingRunClient(
                on_abort=assert_journal_before_abort,
                abort_response={"sessionID": "ses_plan", "accepted": True, "status": "aborted"},
            )
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            result = service.abort_worker("demo", "planner")
            run = store.load_run("demo")

        self.assertEqual(result.abort["status"], "aborted")
        self.assertNotIn(REMOTE_MUTATION_JOURNAL_FIELD, run)
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["workers"]["planner"]["status"], "aborted")
        self.assertEqual(len(client.requests), 1)

    def test_abort_keeps_journal_when_final_abort_persistence_fails_after_api_success(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = _active_worker_store(store_root, directory)
            store = FailingUpdateStore(inner_store, fail_on_update=2)
            client = RecordingRunClient(
                abort_response={"sessionID": "ses_plan", "accepted": True, "status": "aborted"}
            )
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            with self.assertRaisesRegex(RunStoreError, "forced update failure"):
                service.abort_worker("demo", "planner")
            run = inner_store.load_run("demo")

        self.assertEqual(client.requests, [("abort", "ses_plan")])
        self.assertEqual(run["workers"]["planner"]["status"], "active")
        self.assertEqual(len(run[REMOTE_MUTATION_JOURNAL_FIELD]), 1)
        journal = run[REMOTE_MUTATION_JOURNAL_FIELD][0]
        self.assertEqual(journal["kind"], "abort_worker")
        self.assertEqual(journal["session_id"], "ses_plan")

    def test_abort_marks_journal_when_discard_cleanup_fails_after_remote_error(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            inner_store = _active_worker_store(store_root, directory)
            store = FailingUpdateStore(inner_store, fail_on_update=2)

            def reject_abort(session_id):
                raise RuntimeError("remote abort rejected")

            client = RecordingRunClient(on_abort=reject_abort)
            service = RunCommandService(
                store,
                client_factory=lambda url: client,
                now=lambda: "2026-07-05T00:00:00Z",
            )

            with self.assertRaisesRegex(RuntimeError, "remote abort rejected"):
                service.abort_worker("demo", "planner")
            run = inner_store.load_run("demo")

        self.assertEqual(client.requests, [("abort", "ses_plan")])
        self.assertEqual(run["workers"]["planner"]["status"], "active")
        self.assertEqual(len(run[REMOTE_MUTATION_JOURNAL_FIELD]), 1)
        journal = run[REMOTE_MUTATION_JOURNAL_FIELD][0]
        self.assertEqual(journal["kind"], "abort_worker")
        self.assertEqual(journal["session_id"], "ses_plan")
        self.assertEqual(journal["cleanup_failure"]["operation"], "discard_remote_mutation")
        self.assertEqual(journal["cleanup_failure"]["error_type"], "RunStoreError")
        self.assertEqual(journal["cleanup_failure"]["message"], "forced update failure")
        self.assertEqual(journal["cleanup_failure"]["recorded_at"], "2026-07-05T00:00:00Z")


def _active_worker_store(store_root, directory):
    store = RunStore(store_root)
    store.create_run("demo", directory=directory, server_url="http://opencode.example")
    store.upsert_worker(
        "demo",
        "planner",
        role="plan",
        prompt="Plan",
        session_id="ses_plan",
        lifecycle_state="active_wait",
    )
    return store


if __name__ == "__main__":
    unittest.main()
