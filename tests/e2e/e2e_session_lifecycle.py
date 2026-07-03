import json
import os
import tempfile
import unittest
import uuid

from harness import assert_success, format_completed_process, load_json, require_server_url, run_ocs


SERVER_ENV = "OCS_E2E_SERVER_URL"
SESSION_MARKER_PREFIX = "ocs-e2e-lifecycle-"


@unittest.skipUnless(os.environ.get(SERVER_ENV), f"set {SERVER_ENV} to run OpenCode E2E tests")
class SessionLifecycleE2ETest(unittest.TestCase):
    def test_session_lifecycle_through_public_cli_without_prompt_execution(self):
        server_url = require_server_url(self)
        marker = f"{SESSION_MARKER_PREFIX}{uuid.uuid4().hex}"
        session_id = None
        deleted = False

        with tempfile.TemporaryDirectory(prefix=f"{marker}-") as directory:
            try:
                create_result = run_ocs("create", directory, "--server", server_url, "--json")
                assert_success(self, create_result)
                created_session = load_json(self, create_result, "create --json")
                session_id = self._session_id(created_session, "create payload")
                self.assertIn(marker, directory)

                list_result = run_ocs("list", "--directory", directory, "--server", server_url, "--json")
                assert_success(self, list_result)
                listed_sessions = load_json(self, list_result, "list --json")
                self.assertIsInstance(listed_sessions, list, self._context(listed_sessions))
                matching_sessions = [
                    session
                    for session in listed_sessions
                    if self._session_id(session, "list payload") == session_id
                ]
                self.assertEqual(len(matching_sessions), 1, self._context(listed_sessions))
                self.assertEqual(self._session_directory(matching_sessions[0]), directory, self._context(matching_sessions[0]))

                inspect_result = run_ocs("inspect", session_id, "--server", server_url, "--json")
                assert_success(self, inspect_result)
                inspected_session = load_json(self, inspect_result, "inspect --json")
                self.assertEqual(self._session_id(inspected_session, "inspect payload"), session_id)
                self.assertEqual(self._session_directory(inspected_session), directory, self._context(inspected_session))

                get_result = run_ocs("get", session_id, "--server", server_url, "--json")
                assert_success(self, get_result)
                got_session = load_json(self, get_result, "get --json")
                self.assertEqual(self._session_id(got_session, "get payload"), session_id)
                self.assertEqual(self._session_directory(got_session), directory, self._context(got_session))

                delete_result = run_ocs("delete", session_id, "--server", server_url, "--json")
                assert_success(self, delete_result)
                deleted = True
                delete_payload = load_json(self, delete_result, "delete --json")
                self.assertEqual(delete_payload.get("id"), session_id, self._context(delete_payload))
                self.assertTrue(delete_payload.get("deleted"), self._context(delete_payload))
                self.assertEqual(delete_payload.get("verified"), "unreadable", self._context(delete_payload))

                unreadable_result = run_ocs("inspect", session_id, "--server", server_url, "--json")
                self.assertNotEqual(unreadable_result.returncode, 0, format_completed_process(unreadable_result))
                self.assertEqual(unreadable_result.stdout, "", format_completed_process(unreadable_result))
                self.assertIn(session_id, unreadable_result.stderr, format_completed_process(unreadable_result))
            finally:
                if session_id and not deleted:
                    try:
                        run_ocs("delete", session_id, "--server", server_url, "--json")
                    except AssertionError:
                        pass

    def _session_id(self, session, label):
        if not isinstance(session, dict):
            self.fail(f"{label} was not a JSON object:\n{self._context(session)}")
        for name in ("id", "sessionID", "sessionId"):
            value = session.get(name)
            if value:
                return value
        self.fail(f"{label} did not include a session id:\n{self._context(session)}")

    def _session_directory(self, session):
        if not isinstance(session, dict):
            return None
        return session.get("directory") or session.get("cwd")

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
