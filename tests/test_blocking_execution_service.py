import unittest


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


class BlockingExecutionServiceTest(unittest.TestCase):
    def test_prefers_modern_session_message_and_returns_normalized_result(self):
        from opencode_session.blocking_execution import (
            blocking_execution_capabilities,
            blocking_execution_strategy,
            execute_blocking_prompt,
        )

        capabilities = blocking_execution_capabilities(
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
        from opencode_session.blocking_execution import blocking_execution_capabilities, execute_blocking_prompt

        capabilities = blocking_execution_capabilities({"paths": {"/session/{sessionID}/message": {"post": {}}}})
        client = _ExplicitTimeoutClient()

        execute_blocking_prompt(client, "ses_service", "Finish the worker task", capabilities)

        self.assertEqual(client.requests, [("message", 120, 3)])
        self.assertEqual(client.timeout, 3)


if __name__ == "__main__":
    unittest.main()
