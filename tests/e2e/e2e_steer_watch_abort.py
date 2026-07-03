import json
import tempfile
import unittest
import uuid

from harness import (
    add_session_cleanup,
    assert_success,
    format_completed_process,
    load_json,
    load_json_lines,
    require_server_url,
    run_ocs,
)


SESSION_MARKER_PREFIX = "ocs-e2e-steer-watch-abort-"
WATCH_TIMEOUT_SECONDS = "1.0"
STATUS_TERMS = {"queued", "active", "blocked", "done", "failed", "aborted", "timeout"}


class RealServerSteerWatchAbortE2ETest(unittest.TestCase):
    def test_steer_json_admits_durable_prompt_without_execution_claim(self):
        server_url = require_server_url(self)
        marker = f"{SESSION_MARKER_PREFIX}{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-") as directory:
            session_id = self._create_disposable_session(server_url, directory)
            message_id = f"msg_{marker}-steer"

            result = run_ocs(
                "steer",
                session_id,
                f"real-server e2e admission marker {marker}",
                "--message-id",
                message_id,
                "--json",
                "--server",
                server_url,
            )

        assert_success(self, result)
        admission = load_json(self, result, "steer --json")
        self._assert_admission(admission, session_id, message_id)

    def test_watch_json_timeout_contract_after_steer_admission(self):
        server_url = require_server_url(self)
        marker = f"{SESSION_MARKER_PREFIX}{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-") as directory:
            session_id = self._create_disposable_session(server_url, directory)
            steer_result = run_ocs(
                "steer",
                session_id,
                f"real-server e2e watch marker {marker}",
                "--message-id",
                f"msg_{marker}-watch-steer",
                "--json",
                "--server",
                server_url,
            )
            assert_success(self, steer_result)

            watch_result = run_ocs(
                "watch",
                session_id,
                "--json",
                "--timeout",
                WATCH_TIMEOUT_SECONDS,
                "--server",
                server_url,
            )

        self._assert_watch_json_or_timeout(watch_result, session_id)

    def test_abort_json_reports_supported_metadata_for_disposable_session(self):
        server_url = require_server_url(self)
        marker = f"{SESSION_MARKER_PREFIX}{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-") as directory:
            session_id = self._create_disposable_session(server_url, directory)

            result = run_ocs("abort", session_id, "--json", "--server", server_url)

        assert_success(self, result)
        abort = load_json(self, result, "abort --json")
        self._assert_abort(abort, session_id)

    def _create_disposable_session(self, server_url, directory):
        create_result = run_ocs("create", directory, "--json", "--server", server_url)
        assert_success(self, create_result)
        created_session = load_json(self, create_result, "create --json")
        session_id = self._session_id(created_session, "create payload")
        add_session_cleanup(self, server_url, session_id, label="steer/watch/abort session")
        return session_id

    def _assert_admission(self, admission, session_id, message_id):
        self.assertIsInstance(admission, dict, self._context(admission))
        self.assertEqual(admission.get("session_id"), session_id, self._context(admission))
        self.assertEqual(admission.get("message_id"), message_id, self._context(admission))
        self.assertEqual(admission.get("delivery"), "steer", self._context(admission))
        self.assertIn(admission.get("status"), STATUS_TERMS, self._context(admission))
        self.assertIsNone(admission.get("terminal_state"), self._context(admission))
        self.assertFalse((admission.get("fallback") or {}).get("used"), self._context(admission))
        for execution_field in ("executed", "message_ids", "assistant_message_id", "text", "tokens", "cost"):
            self.assertNotIn(execution_field, admission, self._context(admission))

    def _assert_watch_json_or_timeout(self, result, session_id):
        events = load_json_lines(self, result, "watch --json") if result.stdout else []
        for event in events:
            self.assertIsInstance(event, dict, self._context(event))
            self.assertEqual(event.get("session_id"), session_id, self._context(event))
            self.assertIsInstance(event.get("kind"), str, self._context(event))

        if result.returncode == 124:
            self.assertIn(
                f"watch timed out after {WATCH_TIMEOUT_SECONDS}s",
                result.stderr,
                format_completed_process(result),
            )
            return

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stderr, "", format_completed_process(result))
        self.assertGreater(
            len(events),
            0,
            "watch returned success without JSON events\n" + format_completed_process(result),
        )

    def _assert_abort(self, abort, session_id):
        self.assertIsInstance(abort, dict, self._context(abort))
        self.assertEqual(abort.get("session_id"), session_id, self._context(abort))
        self.assertIsInstance(abort.get("accepted"), bool, self._context(abort))
        self.assertIn("status", abort, self._context(abort))
        if abort.get("status") is not None:
            self.assertIn(abort.get("status"), STATUS_TERMS, self._context(abort))
        self.assertIn("raw_status", abort, self._context(abort))
        self.assertIn("response", abort, self._context(abort))

    def _session_id(self, session, label):
        if isinstance(session, dict) and isinstance(session.get("data"), dict):
            session = session["data"]
        if not isinstance(session, dict):
            self.fail(f"{label} was not a JSON object:\n{self._context(session)}")
        for name in ("id", "sessionID", "sessionId"):
            value = session.get(name)
            if value:
                return value
        self.fail(f"{label} did not include a session id:\n{self._context(session)}")

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
