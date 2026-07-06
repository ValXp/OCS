import argparse
import unittest

from opencode_session.api_profile import OpenCodeServerProfile
from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.capabilities import configure_client_route_plan, detect_capabilities

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs


def capability_server(*, health=None, doc=None, health_path="/global/health"):
    server = FakeOpenCodeServer()
    server.json("GET", health_path, health or {"status": "ok", "version": "1.2.3"})
    server.json("GET", "/doc", doc or {"openapi": "3.1.0", "paths": {}})
    return server


class CapabilityProbeCliTest(unittest.TestCase):
    def test_capabilities_parser_installs_command_handler(self):
        from opencode_session.commands.capabilities import add_capabilities_parser

        parser = argparse.ArgumentParser(prog="ocs")
        subparsers = parser.add_subparsers(dest="command")

        def handler(args):
            return args

        add_capabilities_parser(
            subparsers,
            add_server_argument=lambda command_parser: command_parser.add_argument("--server", default="default-server"),
            handler=handler,
        )

        args = parser.parse_args(["capabilities", "--server", "http://example.test", "--json"])
        self.assertEqual(args.command, "capabilities")
        self.assertEqual(args.server, "http://example.test")
        self.assertTrue(args.json)
        self.assertTrue(callable(args.command_handler))
        self.assertIs(args.command_handler, handler)

    def test_compact_summary_reports_detected_paths(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
                "/session/{sessionID}/message": {"post": {}},
            },
        }

        with capability_server(doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout.strip(),
            "health=ok version=1.2.3 session=/api/session prompt=/api/session/{sessionID}/prompt "
            "wait=/api/session/{sessionID}/wait events=/api/event execution=/session/{sessionID}/message "
            "legacy=unsupported",
        )

    def test_json_output_exposes_capability_contract(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
                "/session/{sessionID}/message": {"post": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }
        health = {"status": "ok", "version": "2.0.0"}

        with capability_server(health=health, doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = load_json(self, result)
        self.assertEqual(payload["health"], "ok")
        self.assertEqual(payload["version"], "2.0.0")
        self.assertTrue(payload["v2_prompt_support"])
        self.assertTrue(payload["v2_wait_support"])
        self.assertTrue(payload["event_support"])
        self.assertTrue(payload["blocking_message_available"])
        self.assertTrue(payload["blocking_execution_available"])
        self.assertTrue(payload["legacy_fallback_available"])
        self.assertEqual(
            payload["route_availability"],
            {
                "session": {"path": "/api/session", "method": "POST", "available": True},
                "v2_prompt": {
                    "path": "/api/session/{sessionID}/prompt",
                    "method": "POST",
                    "available": True,
                },
                "v2_wait": {
                    "path": "/api/session/{sessionID}/wait",
                    "method": "POST",
                    "available": True,
                },
                "events": {"path": "/api/event", "method": "GET", "available": True},
                "blocking_message": {
                    "path": "/session/{sessionID}/message",
                    "method": "POST",
                    "available": True,
                },
                "legacy_run": {
                    "path": "/session/{sessionID}/run",
                    "method": "POST",
                    "available": True,
                },
                "legacy_reply": {
                    "path": "/session/{sessionID}/reply",
                    "method": "POST",
                    "available": True,
                },
            },
        )
        self.assertEqual(
            payload["route_plan"],
            {
                "session_collection": "/api/session",
                "session_item": "/api/session/{sessionID}",
                "v2_prompt": "/api/session/{sessionID}/prompt",
                "v2_wait": "/api/session/{sessionID}/wait",
                "events": "/api/event",
                "blocking_message": "/session/{sessionID}/message",
                "legacy_run": "/session/{sessionID}/run",
                "legacy_reply": "/session/{sessionID}/reply",
            },
        )

    def test_server_profile_selects_api_routes_and_adapters(self):
        profile = OpenCodeServerProfile.from_openapi_doc(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/api/session": {"get": {}, "post": {}},
                    "/api/session/{sessionID}/prompt": {"post": {}},
                    "/api/event": {"get": {}},
                    "/session/{sessionID}/message": {"post": {}},
                },
            },
            health={"status": "ok", "version": "2.0.0"},
        )

        session_payload = {"sessions": [{"sessionID": "ses_legacy", "name": "Legacy"}]}
        event = {"event": "session.status", "payload": {"sessionID": "ses_1", "status": "completed"}}

        self.assertEqual(profile.health, "ok")
        self.assertEqual(profile.version, "2.0.0")
        self.assertEqual(profile.route_plan["session_collection"], "/api/session")
        self.assertEqual(profile.adapter("session_collection").version, "api-v1")
        self.assertEqual(profile.adapter("events").version, "api-v1")
        self.assertEqual(profile.adapter("blocking_message").version, "session-message")
        self.assertEqual(profile.normalize_session_payload(session_payload)["sessions"][0]["schema_status"], "unknown")
        self.assertEqual(profile.normalize_event_record(event, "ses_1")["schema_status"], "unknown")
        self.assertEqual(
            profile.prompt_admission_payload("msg_1", "PONG", "queue"),
            {"id": "msg_1", "prompt": {"text": "PONG"}, "delivery": "queue"},
        )

    def test_server_profile_selects_legacy_routes_and_adapters(self):
        profile = OpenCodeServerProfile.from_openapi_doc(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/session": {"get": {}, "post": {}},
                    "/session/{sessionID}/prompt_async": {"post": {}},
                    "/event": {"get": {}},
                    "/session/{sessionID}/run": {"post": {}},
                    "/session/{sessionID}/reply": {"post": {}},
                },
            }
        )

        session_payload = {"sessions": [{"sessionID": "ses_legacy", "name": "Legacy"}]}
        event = {"event": "session.status", "payload": {"sessionID": "ses_1", "status": "completed"}}

        self.assertEqual(profile.route_plan["session_collection"], "/session")
        self.assertEqual(profile.route_plan["session_item"], "/session/{sessionID}")
        self.assertEqual(profile.adapter("session_collection").version, "legacy")
        self.assertEqual(profile.adapter("events").version, "legacy")
        self.assertEqual(profile.adapter("legacy_run").version, "legacy-run-reply")
        self.assertEqual(profile.normalize_session_payload(session_payload)["sessions"][0]["id"], "ses_legacy")
        self.assertEqual(profile.normalize_event_record(event, "ses_1")["kind"], "status")
        self.assertEqual(
            profile.prompt_admission_payload("msg_1", "PONG", "queue"),
            {"messageID": "msg_1", "parts": [{"type": "text", "text": "PONG"}], "delivery": "queue"},
        )

    def test_detected_session_route_plan_drives_client_session_routes(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/session": {"get": {}, "post": {}},
                "/session/{sessionID}": {"get": {}, "delete": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }

        with capability_server(doc=doc) as server:
            server.json("POST", "/session", {"id": "ses_1", "directory": "/tmp/project"})
            server.json("GET", "/session", {"sessions": [{"id": "ses_1", "directory": "/tmp/project"}]})
            server.json("GET", "/session/ses_1", {"id": "ses_1", "directory": "/tmp/project"})
            server.json("DELETE", "/session/ses_1", {"id": "ses_1", "deleted": True})
            client = OpenCodeApiClient(server.url)

            capabilities = detect_capabilities(client)
            configure_client_route_plan(client, capabilities)
            created = client.create_session_response("/tmp/project")
            listed = client.list_sessions_response()
            inspected = client.get_session_response("ses_1")
            deleted = client.delete_session_response("ses_1")
            requests = list(server.requests)

        self.assertEqual(capabilities["route_plan"]["session_collection"], "/session")
        self.assertEqual(client.server_profile.adapter("session_collection").version, "legacy")
        self.assertEqual(created.data["id"], "ses_1")
        self.assertEqual(listed.data["sessions"][0]["id"], "ses_1")
        self.assertEqual(inspected.data["id"], "ses_1")
        self.assertEqual(deleted.data["deleted"], True)
        self.assertEqual(
            [(method, path) for method, path, _payload in requests],
            [
                ("GET", "/global/health"),
                ("GET", "/doc"),
                ("POST", "/session"),
                ("GET", "/session"),
                ("GET", "/session/ses_1"),
                ("DELETE", "/session/ses_1"),
            ],
        )

    def test_profile_route_plan_drives_auxiliary_domain_endpoints(self):
        with FakeOpenCodeServer() as server:
            server.json("POST", "/custom/ses_1/abort", {"id": "ses_1", "aborted": True})
            server.json("POST", "/custom/ses_1/fork", {"id": "ses_child"})
            server.json("GET", "/custom/ses_1/children", {"children": [{"sessionID": "ses_child", "name": "Child"}]})
            server.json("GET", "/custom/permission", {"permissions": []})
            server.json("POST", "/custom/permission/perm_1/reply", {"id": "perm_1", "reply": "allow"})
            server.json("GET", "/custom/question", {"questions": []})
            server.json("POST", "/custom/question/q_1/reply", {"id": "q_1", "answers": ["yes"]})
            server.json("POST", "/custom/question/q_1/reject", {"id": "q_1", "rejected": True})
            client = OpenCodeApiClient(server.url).configure_route_plan(
                {
                    "session_abort": "/custom/{sessionID}/abort",
                    "session_fork": "/custom/{sessionID}/fork",
                    "session_children": "/custom/{sessionID}/children",
                    "permissions": "/custom/permission",
                    "permission_reply": "/custom/permission/{requestID}/reply",
                    "questions": "/custom/question",
                    "question_reply": "/custom/question/{requestID}/reply",
                    "question_reject": "/custom/question/{requestID}/reject",
                }
            )

            client.abort_session_response("ses_1")
            client.fork_session_response("ses_1", message_id="msg_1")
            children = client.list_child_sessions_response("ses_1")
            client.list_permissions_response()
            client.reply_permission_response("perm_1", "allow")
            client.list_questions_response()
            client.answer_question_response("q_1", ["yes"])
            client.reject_question_response("q_1")
            requests = list(server.requests)

        self.assertEqual(children.data["children"][0]["id"], "ses_child")
        self.assertEqual(
            [(method, path) for method, path, _payload in requests],
            [
                ("POST", "/custom/ses_1/abort"),
                ("POST", "/custom/ses_1/fork"),
                ("GET", "/custom/ses_1/children"),
                ("GET", "/custom/permission"),
                ("POST", "/custom/permission/perm_1/reply"),
                ("GET", "/custom/question"),
                ("POST", "/custom/question/q_1/reply"),
                ("POST", "/custom/question/q_1/reject"),
            ],
        )

    def test_route_dependent_client_calls_require_configured_route_plan(self):
        client = OpenCodeApiClient("http://127.0.0.1:4096")

        with self.assertRaisesRegex(OpenCodeApiError, "route plan is not configured"):
            client.list_sessions_response()

    def test_configured_route_plan_drives_client_blocking_execution_routes(self):
        with FakeOpenCodeServer() as server:
            server.json("POST", "/custom/ses_1/message", {"id": "msg_assistant", "status": "completed"})
            server.json("POST", "/custom/ses_1/run", {"id": "msg_user", "status": "submitted"})
            server.json("POST", "/custom/ses_1/reply", {"id": "msg_assistant", "status": "completed"})
            client = OpenCodeApiClient(server.url).configure_route_plan(
                {
                    "blocking_message": "/custom/{sessionID}/message",
                    "legacy_run": "/custom/{sessionID}/run",
                    "legacy_reply": "/custom/{sessionID}/reply",
                }
            )

            client.message_session_response("ses_1", "Finish the worker task")
            client.run_session_response("ses_1", "Finish the worker task")
            client.reply_session_response("ses_1")
            requests = list(server.requests)

        self.assertEqual(
            [(method, path) for method, path, _payload in requests],
            [
                ("POST", "/custom/ses_1/message"),
                ("POST", "/custom/ses_1/run"),
                ("POST", "/custom/ses_1/reply"),
            ],
        )

    def test_delete_session_does_not_fallback_on_invalid_json(self):
        with FakeOpenCodeServer() as server:
            server.route("DELETE", "/api/session/ses_1", lambda handler, _request: handler._write_text("deleted"))
            server.json("DELETE", "/session/ses_1", {"id": "ses_1", "deleted": True})
            client = OpenCodeApiClient(server.url).configure_route_plan({})

            with self.assertRaisesRegex(OpenCodeApiError, "returned invalid JSON"):
                client.delete_session_response("ses_1")
            requests = list(server.requests)

        self.assertEqual([(method, path) for method, path, _payload in requests], [("DELETE", "/api/session/ses_1")])

    def test_unsupported_server_has_stable_exit_and_clear_error(self):
        doc = {"openapi": "3.1.0", "paths": {"/unrelated": {"get": {}}}}

        with capability_server(doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url)

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported OpenCode server", result.stderr)
        self.assertIn("missing session control: POST /api/session or POST /session", result.stderr)
        self.assertIn(
            "missing prompt admission or blocking execution: POST /api/session/{sessionID}/prompt, POST /session/{sessionID}/message, or legacy POST /session/{sessionID}/run + POST /session/{sessionID}/reply",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
