import json
import os
import re
import tempfile
import unittest
import uuid

from harness import (
    assert_success,
    format_completed_process,
    load_json,
    require_server_url,
    run_ocs,
)


SERVER_ENV = "OCS_E2E_SERVER_URL"


@unittest.skipUnless(os.environ.get(SERVER_ENV), f"set {SERVER_ENV} to run OpenCode E2E tests")
class BlockerCommandsE2ETest(unittest.TestCase):
    def test_permission_list_json_accepts_real_server_response(self):
        server_url = require_server_url(self)

        result = run_ocs("permission", "list", "--json", "--server", server_url)

        assert_success(self, result)
        permissions = load_json(self, result, "permission list --json")
        context = self._context(permissions)
        self.assertIsInstance(permissions, list, context)
        for permission in permissions:
            self.assertIsInstance(permission, dict, context)

    def test_question_list_json_accepts_real_server_response(self):
        server_url = require_server_url(self)

        result = run_ocs("question", "list", "--json", "--server", server_url)

        assert_success(self, result)
        questions = load_json(self, result, "question list --json")
        context = self._context(questions)
        self.assertIsInstance(questions, list, context)
        for question in questions:
            self.assertIsInstance(question, dict, context)

    def test_session_list_and_inspect_include_blocker_counts_without_active_blockers(self):
        server_url = require_server_url(self)
        marker = f"ocs-e2e-blockers-{uuid.uuid4().hex}"
        session_id = None
        deleted = False

        with tempfile.TemporaryDirectory(prefix=f"{marker}-") as directory:
            try:
                create_result = run_ocs("create", directory, "--json", "--server", server_url)
                assert_success(self, create_result)
                created_session = load_json(self, create_result, "create --json")
                session_id = self._session_id(created_session, "create payload")

                list_result = run_ocs(
                    "list",
                    "--directory",
                    directory,
                    "--blockers",
                    "--json",
                    "--server",
                    server_url,
                )
                assert_success(self, list_result)
                listed_sessions = load_json(self, list_result, "list --blockers --json")
                listed_session = self._only_session(
                    listed_sessions,
                    session_id,
                    "list --blockers --json payload",
                )
                self._assert_blocker_counts(
                    listed_session.get("blockers"),
                    self._context(listed_session),
                )

                inspect_result = run_ocs("inspect", session_id, "--blockers", "--server", server_url)
                assert_success(self, inspect_result)
                self.assertEqual(inspect_result.stderr, "", format_completed_process(inspect_result))
                self._assert_compact_blocker_counts(inspect_result.stdout, session_id)

                delete_result = run_ocs("delete", session_id, "--json", "--server", server_url)
                assert_success(self, delete_result)
                deleted = True
            finally:
                if session_id and not deleted:
                    try:
                        run_ocs("delete", session_id, "--json", "--server", server_url)
                    except AssertionError:
                        pass

    def _only_session(self, sessions, session_id, label):
        if not isinstance(sessions, list):
            self.fail(f"{label} was not a JSON array:\n{self._context(sessions)}")
        matches = [
            session
            for session in sessions
            if isinstance(session, dict) and self._session_id_or_none(session) == session_id
        ]
        self.assertEqual(len(matches), 1, self._context(sessions))
        return matches[0]

    def _assert_blocker_counts(self, blockers, context):
        self.assertIsInstance(blockers, dict, context)
        counts = {}
        for name in ("permissions", "questions", "total"):
            self.assertIn(name, blockers, context)
            self.assertIsInstance(blockers[name], int, context)
            self.assertGreaterEqual(blockers[name], 0, context)
            counts[name] = blockers[name]
        self.assertEqual(counts["total"], counts["permissions"] + counts["questions"], context)

    def _assert_compact_blocker_counts(self, stdout, session_id):
        line = stdout.strip()
        self.assertNotEqual(line, "", stdout)
        self.assertEqual(stdout, line + "\n", stdout)
        self.assertRegex(line, rf"(^| )id={re.escape(session_id)}( |$)", line)
        counts = {}
        for name in ("permissions", "questions", "blockers"):
            match = re.search(rf"(^| ){name}=(\d+)( |$)", line)
            self.assertIsNotNone(match, line)
            counts[name] = int(match.group(2))
        self.assertEqual(counts["blockers"], counts["permissions"] + counts["questions"], line)

    def _session_id(self, session, label):
        if not isinstance(session, dict):
            self.fail(f"{label} was not a JSON object:\n{self._context(session)}")
        session_id = self._session_id_or_none(session)
        if session_id:
            return session_id
        self.fail(f"{label} did not include a session id:\n{self._context(session)}")

    def _session_id_or_none(self, session):
        for name in ("id", "sessionID", "sessionId"):
            value = session.get(name)
            if value:
                return value
        return None

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
