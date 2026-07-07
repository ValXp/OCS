import unittest
from unittest.mock import patch


class _Response:
    def __init__(self, data):
        self.data = data


class _BlockingExecutionClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None):
        self.requests.append(("message", session_id, message, message_id, timeout))
        return _Response(
            {
                "info": {
                    "id": "msg_assistant_service",
                    "cost": 0.02,
                    "tokens": {"input": 4, "output": 2, "total": 6},
                },
                "parts": [{"type": "text", "text": "PONG"}],
            }
        )

    def run_session_response(self, session_id, message, *, timeout=None):
        self.requests.append(("run", session_id, message, None, timeout))
        return _Response({"id": "msg_user_legacy", "status": "submitted"})

    def reply_session_response(self, session_id, *, timeout=None):
        self.requests.append(("reply", session_id, None, None, timeout))
        return _Response({"id": "msg_assistant_legacy", "status": "completed", "text": "legacy"})


class _ExplicitTimeoutClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None):
        self.requests.append(("message", timeout, self.timeout))
        return _Response({"id": "msg_assistant_explicit", "status": "completed", "text": "ok"})


class _DeadlineClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def run_session_response(self, session_id, message, *, timeout=None, deadline=None):
        self.requests.append(("run", session_id, message, timeout, deadline is not None))
        return _Response({"id": "msg_user_deadline", "status": "submitted"})

    def reply_session_response(self, session_id, *, timeout=None, deadline=None):
        self.requests.append(("reply", session_id, None, timeout, deadline is not None))
        return _Response({"id": "msg_assistant_deadline", "status": "completed", "text": "ok"})


class _UnknownSessionMessageClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None):
        self.requests.append(("message", session_id, message, message_id, timeout))
        return _Response({"unexpected": {"shape": True}})


class _PayloadSessionMessageClient:
    def __init__(self, payload):
        self.timeout = 3
        self.payload = payload
        self.requests = []

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None):
        self.requests.append(("message", session_id, message, message_id, timeout))
        return _Response(self.payload)


class _UnknownLegacyRunClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def run_session_response(self, session_id, message, *, timeout=None):
        self.requests.append(("run", session_id, message, None, timeout))
        return _Response({"tokenUsage": {"input": 1}})

    def reply_session_response(self, session_id, *, timeout=None):
        raise AssertionError("reply should not be requested after unknown run schema")


class _UnknownLegacyReplyClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def run_session_response(self, session_id, message, *, timeout=None):
        self.requests.append(("run", session_id, message, None, timeout))
        return _Response({"id": "msg_user_legacy", "status": "submitted"})

    def reply_session_response(self, session_id, *, timeout=None):
        self.requests.append(("reply", session_id, None, None, timeout))
        return _Response({"tokenUsage": {"output": 1}})


class _IncompleteLegacyRunClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def run_session_response(self, session_id, message, *, timeout=None):
        self.requests.append(("run", session_id, message, None, timeout))
        return _Response({"status": "submitted"})

    def reply_session_response(self, session_id, *, timeout=None):
        raise AssertionError("reply should not be requested after incomplete run schema")


class _IncompleteLegacyReplyClient:
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def run_session_response(self, session_id, message, *, timeout=None):
        self.requests.append(("run", session_id, message, None, timeout))
        return _Response({"id": "msg_user_legacy", "status": "submitted"})

    def reply_session_response(self, session_id, *, timeout=None):
        self.requests.append(("reply", session_id, None, None, timeout))
        return _Response({"text": "legacy finished"})


class BlockingExecutionServiceTest(unittest.TestCase):
    def test_prefers_modern_session_message_and_returns_normalized_result(self):
        from opencode_session.blocking_execution import (
            blocking_execution_strategy,
            execute_blocking_prompt,
        )
        from opencode_session.capabilities import capabilities_from_openapi_doc

        capabilities = capabilities_from_openapi_doc(
            {
                "paths": {
                    "/session/{sessionID}/message": {"post": {}},
                    "/session/{sessionID}/run": {"post": {}},
                    "/session/{sessionID}/reply": {"post": {}},
                }
            }
        )
        client = _BlockingExecutionClient()

        result = execute_blocking_prompt(client, "ses_service", "Reply exactly PONG.", capabilities)

        self.assertEqual(blocking_execution_strategy(capabilities), "session_message")
        self.assertEqual([request[0] for request in client.requests], ["message"])
        self.assertEqual(client.timeout, 3)
        self.assertTrue(client.requests[0][3].startswith("msg_"))
        self.assertGreaterEqual(client.requests[0][4], 120)
        self.assertEqual(
            result,
            {
                "session_id": "ses_service",
                "message_ids": {"user": client.requests[0][3], "assistant": "msg_assistant_service"},
                "status": "done",
                "raw_status": "completed",
                "terminal_state": "done",
                "api_path": {"message": "/session/{sessionID}/message"},
                "execution_strategy": "session_message",
                "fallback": {"available": True, "strategy": "legacy_run_reply", "used": False},
                "cost": 0.02,
                "tokens": {"input": 4, "output": 2, "total": 6},
                "text": "PONG",
            },
        )

    def test_uses_explicit_request_timeout_without_mutating_client_default(self):
        from opencode_session.blocking_execution import execute_blocking_prompt
        from opencode_session.capabilities import capabilities_from_openapi_doc

        capabilities = capabilities_from_openapi_doc({"paths": {"/session/{sessionID}/message": {"post": {}}}})
        client = _ExplicitTimeoutClient()

        execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertEqual(client.requests, [("message", 120, 3)])
        self.assertEqual(client.timeout, 3)

    def test_passes_deadline_to_legacy_run_and_reply_requests(self):
        from opencode_session.blocking_execution import execute_blocking_prompt
        from opencode_session.timeout_boundary import TimeoutDeadline

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _DeadlineClient()

        execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities, deadline=TimeoutDeadline(5))

        self.assertEqual([request[0] for request in client.requests], ["run", "reply"])
        self.assertTrue(all(request[4] for request in client.requests))
        self.assertTrue(all(0 < request[3] <= 5 for request in client.requests))

    def test_reports_route_plan_paths_for_legacy_execution(self):
        from opencode_session.blocking_execution import execute_blocking_prompt

        capabilities = {
            "route_availability": {},
            "legacy_fallback_available": True,
            "route_plan": {
                "legacy_run": "/custom/{sessionID}/run",
                "legacy_reply": "/custom/{sessionID}/reply",
            },
        }
        client = _BlockingExecutionClient()

        result = execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertEqual(
            result["api_path"],
            {"run": "/custom/{sessionID}/run", "reply": "/custom/{sessionID}/reply"},
        )

    def test_provider_failure_extracts_error_only_message(self):
        from opencode_session.blocking_execution import provider_failure
        from opencode_session.schema_message_adapter import SESSION_MESSAGE_ROUTE

        self.assertEqual(
            provider_failure({"data": {"reason": "quota exceeded"}}, route=SESSION_MESSAGE_ROUTE),
            "quota exceeded",
        )

    def test_session_message_execution_normalizes_provider_response_once(self):
        from opencode_session import blocking_execution
        from opencode_session.capabilities import capabilities_from_openapi_doc
        from opencode_session.schema_message_adapter import normalize_message_result

        capabilities = capabilities_from_openapi_doc(
            {"paths": {"/session/{sessionID}/message": {"post": {}}}}
        )
        client = _BlockingExecutionClient()

        with patch.object(
            blocking_execution,
            "normalize_message_result",
            wraps=normalize_message_result,
        ) as normalized:
            blocking_execution.execute_blocking_prompt(client, "ses_service", "Reply exactly PONG.", capabilities)

        self.assertEqual(normalized.call_count, 1)

    def test_legacy_execution_normalizes_each_provider_response_once(self):
        from opencode_session import blocking_execution
        from opencode_session.schema_message_adapter import normalize_message_result

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _BlockingExecutionClient()

        with patch.object(
            blocking_execution,
            "normalize_message_result",
            wraps=normalize_message_result,
        ) as normalized:
            blocking_execution.execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertEqual(normalized.call_count, 2)

    def test_rejects_incomplete_success_shaped_session_message_payloads(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )
        from opencode_session.capabilities import capabilities_from_openapi_doc

        capabilities = capabilities_from_openapi_doc(
            {"paths": {"/session/{sessionID}/message": {"post": {}}}}
        )
        incomplete_payloads = (
            ({"text": "looks done"}, "missing assistant message id"),
            ({"id": "msg_assistant_empty"}, "missing assistant text or explicit terminal status"),
            ({"status": "completed"}, "missing assistant message id"),
        )

        for payload, expected_reason in incomplete_payloads:
            with self.subTest(payload=payload):
                client = _PayloadSessionMessageClient(payload)

                with self.assertRaises(BlockingProviderFailure) as raised:
                    execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

                self.assertIn("incomplete message schema from blocking message response", str(raised.exception))
                self.assertIn(expected_reason, str(raised.exception))
                self.assertTrue(raised.exception.prompt_id.startswith("msg_"))

    def test_status_only_session_message_failure_stays_provider_failure(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )
        from opencode_session.capabilities import capabilities_from_openapi_doc

        capabilities = capabilities_from_openapi_doc(
            {"paths": {"/session/{sessionID}/message": {"post": {}}}}
        )
        client = _PayloadSessionMessageClient({"status": "failed"})

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertEqual(str(raised.exception), "failed")
        self.assertTrue(raised.exception.prompt_id.startswith("msg_"))

    def test_rejects_unknown_session_message_schema_instead_of_completed_result(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )
        from opencode_session.capabilities import capabilities_from_openapi_doc

        capabilities = capabilities_from_openapi_doc(
            {"paths": {"/session/{sessionID}/message": {"post": {}}}}
        )
        client = _UnknownSessionMessageClient()

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertIn(
            "unrecognized message schema from blocking message response",
            str(raised.exception),
        )
        self.assertTrue(raised.exception.prompt_id.startswith("msg_"))

    def test_rejects_unknown_legacy_run_schema_before_reply(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _UnknownLegacyRunClient()

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertIn(
            "unrecognized message schema from legacy run response",
            str(raised.exception),
        )
        self.assertIsNone(raised.exception.prompt_id)
        self.assertEqual([request[0] for request in client.requests], ["run"])

    def test_rejects_unknown_legacy_reply_schema_instead_of_completed_result(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _UnknownLegacyReplyClient()

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertIn(
            "unrecognized message schema from legacy reply response",
            str(raised.exception),
        )
        self.assertEqual(raised.exception.prompt_id, "msg_user_legacy")
        self.assertEqual([request[0] for request in client.requests], ["run", "reply"])

    def test_rejects_incomplete_legacy_run_schema_before_reply(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _IncompleteLegacyRunClient()

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertIn(
            "incomplete message schema from legacy run response: missing user message id",
            str(raised.exception),
        )
        self.assertIsNone(raised.exception.prompt_id)
        self.assertEqual([request[0] for request in client.requests], ["run"])

    def test_rejects_incomplete_legacy_reply_schema_instead_of_completed_result(self):
        from opencode_session.blocking_execution import (
            BlockingProviderFailure,
            execute_blocking_prompt,
        )

        capabilities = {"route_availability": {}, "legacy_fallback_available": True}
        client = _IncompleteLegacyReplyClient()

        with self.assertRaises(BlockingProviderFailure) as raised:
            execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertIn(
            "incomplete message schema from legacy reply response: missing assistant message id",
            str(raised.exception),
        )
        self.assertEqual(raised.exception.prompt_id, "msg_user_legacy")
        self.assertEqual([request[0] for request in client.requests], ["run", "reply"])


if __name__ == "__main__":
    unittest.main()
