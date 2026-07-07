import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from opencode_session.api_transport import OpenCodeApiError, OpenCodeApiResponse
from opencode_session.commands.validation import handle_validation_command
from opencode_session.validation_cleanup import cleanup_stale_disposable_sessions


class FakeCleanupClient:
    def __init__(self, sessions, *, doc_error=None, still_readable=()):
        self.sessions = list(sessions)
        self.doc_error = doc_error
        self.still_readable = set(still_readable)
        self.deleted = set()
        self.calls = []
        self.server_profile = None

    def get_openapi_doc(self):
        self.calls.append(("GET", "/doc"))
        if self.doc_error is not None:
            raise self.doc_error
        return {
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}": {"get": {}, "delete": {}},
            }
        }

    def configure_server_profile(self, profile):
        self.server_profile = profile
        self.calls.append(("CONFIGURE", profile.route_plan["session_collection"]))

    def list_sessions_response(self):
        self.calls.append(("GET", "/api/session"))
        return OpenCodeApiResponse({"sessions": self.sessions}, "{}")

    def delete_session_response(self, session_id):
        self.calls.append(("DELETE", session_id))
        self.deleted.add(session_id)
        return OpenCodeApiResponse({}, "{}")

    def get_session(self, session_id):
        self.calls.append(("GET_SESSION", session_id))
        if session_id in self.still_readable:
            return {"id": session_id}
        if session_id in self.deleted:
            raise OpenCodeApiError(f"GET /api/session/{session_id} failed: HTTP 404", status=404)
        raise OpenCodeApiError(f"session {session_id} not found", status=404)


class ValidationCleanupLogicTest(unittest.TestCase):
    def test_cleanup_selects_matching_disposable_sessions_and_returns_record(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            client = FakeCleanupClient(
                [
                    {"id": "ses_keep", "title": "regular", "directory": directory},
                    {"id": "ses_title", "title": "ocs-smoke-old", "directory": directory},
                    {
                        "id": "ses_metadata",
                        "title": "generated",
                        "directory": directory,
                        "metadata": {"prefix": "ocs-smoke-"},
                    },
                    {"id": "ses_other_dir", "title": "ocs-smoke-other", "directory": other_directory},
                ]
            )

            result = cleanup_stale_disposable_sessions(client, prefix="ocs-smoke-", directory=directory)

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["prefix"], "ocs-smoke-")
        self.assertEqual(result["stale"], 2)
        self.assertEqual(result["sessions"], ["ses_title", "ses_metadata"])
        self.assertEqual(result["deleted"], ["ses_title", "ses_metadata"])
        self.assertEqual(result["verified"], ["ses_title", "ses_metadata"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [call for call in client.calls if call[0] in {"DELETE", "GET_SESSION"}],
            [
                ("DELETE", "ses_title"),
                ("GET_SESSION", "ses_title"),
                ("DELETE", "ses_metadata"),
                ("GET_SESSION", "ses_metadata"),
            ],
        )

    def test_cleanup_records_delete_verification_failure_without_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeCleanupClient(
                [{"id": "ses_stale", "title": "ocs-smoke-old", "directory": directory}],
                still_readable={"ses_stale"},
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = cleanup_stale_disposable_sessions(client, prefix="ocs-smoke-", directory=directory)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["verified"], [])
        self.assertEqual(result["errors"][0]["session_id"], "ses_stale")
        self.assertIn("delete verification failed", result["errors"][0]["error"])


class ValidationCleanupCommandTest(unittest.TestCase):
    def test_cleanup_handler_renders_success_json(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeCleanupClient([{"id": "ses_stale", "title": "ocs-smoke-old", "directory": directory}])
            errors = []
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = handle_validation_command(
                    self._cleanup_args(directory, json=True),
                    print_error=errors.append,
                    unavailable_exit=69,
                    unsupported_exit=78,
                    dataerr_exit=65,
                    client_factory=self._client_factory(client),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, [])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["prefix"], "ocs-smoke-")
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["stale"], 1)
        self.assertEqual(payload["deleted"], ["ses_stale"])
        self.assertEqual(payload["verified"], ["ses_stale"])

    def test_cleanup_handler_maps_cleanup_failure_to_command_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeCleanupClient(
                [{"id": "ses_stale", "title": "ocs-smoke-old", "directory": directory}],
                still_readable={"ses_stale"},
            )
            errors = []
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = handle_validation_command(
                    self._cleanup_args(directory),
                    print_error=errors.append,
                    unavailable_exit=69,
                    unsupported_exit=78,
                    dataerr_exit=65,
                    client_factory=self._client_factory(client),
                )

        self.assertEqual(exit_code, 69)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            errors,
            [f"cleanup failed: cleanup stale=1 deleted=0 verified=0 prefix=ocs-smoke- dir={directory}"],
        )

    def test_cleanup_handler_maps_api_error_to_command_error(self):
        client = FakeCleanupClient([], doc_error=OpenCodeApiError("doc unavailable"))
        errors = []
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = handle_validation_command(
                self._cleanup_args("."),
                print_error=errors.append,
                unavailable_exit=69,
                unsupported_exit=78,
                dataerr_exit=65,
                client_factory=self._client_factory(client),
            )

        self.assertEqual(exit_code, 69)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(errors, ["doc unavailable"])

    def _cleanup_args(self, directory, *, json=False):
        return SimpleNamespace(
            command="cleanup",
            server="http://opencode.invalid",
            directory=directory,
            prefix="ocs-smoke-",
            json=json,
        )

    def _client_factory(self, client):
        def create(server):
            self.assertEqual(server, "http://opencode.invalid")
            return client

        return create


if __name__ == "__main__":
    unittest.main()
