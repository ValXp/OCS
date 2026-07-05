import unittest

from opencode_session.schema_normalization import (
    iter_normalized_message_records,
    normalize_admission_record,
    normalize_event_record,
    normalize_session_payload,
)


class SchemaNormalizationTest(unittest.TestCase):
    def test_normalizes_session_aliases_in_wrapped_collections(self):
        payload = {
            "sessions": [
                {
                    "sessionID": "ses_1",
                    "name": "Build",
                    "location": {"directory": "/tmp/project"},
                    "agentID": "build",
                    "modelID": "openai/gpt-5.5",
                    "tokenUsage": {"input": 2, "output": 3},
                    "time": {"created": "2026-07-01T00:00:00Z", "updated": "2026-07-01T00:00:03Z"},
                }
            ]
        }

        normalized = normalize_session_payload(payload)

        session = normalized["sessions"][0]
        self.assertEqual(session["id"], "ses_1")
        self.assertEqual(session["title"], "Build")
        self.assertEqual(session["directory"], "/tmp/project")
        self.assertEqual(session["agent"], "build")
        self.assertEqual(session["model"], "openai/gpt-5.5")
        self.assertEqual(session["tokens"], {"input": 2, "output": 3, "total": 5})
        self.assertEqual(session["createdAt"], "2026-07-01T00:00:00Z")
        self.assertEqual(session["updatedAt"], "2026-07-01T00:00:03Z")

    def test_normalizes_message_evidence_aliases(self):
        payload = {
            "messages": [
                {
                    "messageID": "msg_1",
                    "author": "assistant",
                    "state": "completed",
                    "info": {"cost": 0.1, "tokenUsage": {"input": 1, "output": 2}},
                    "parts": [{"type": "text", "text": "PONG"}],
                }
            ]
        }

        self.assertEqual(
            list(iter_normalized_message_records(payload)),
            [
                {
                    "messageID": "msg_1",
                    "author": "assistant",
                    "state": "completed",
                    "info": {"cost": 0.1, "tokenUsage": {"input": 1, "output": 2}},
                    "parts": [{"type": "text", "text": "PONG"}],
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "done",
                    "raw_status": "completed",
                    "cost": 0.1,
                    "tokens": {"input": 1, "output": 2, "total": 3},
                    "text": "PONG",
                }
            ],
        )

    def test_normalizes_admission_and_event_records(self):
        capabilities = {
            "route_availability": {"v2_prompt": {"path": "/api/session/{sessionID}/prompt"}},
            "legacy_fallback_available": False,
        }

        admission = normalize_admission_record(
            "ses_fallback",
            "steer",
            "msg_fallback",
            {"info": {"sessionID": "ses_1", "promptID": "msg_1"}, "deliveryMode": "queue", "status": "promoted"},
            capabilities=capabilities,
        )
        event = normalize_event_record(
            {
                "type": "permission.requested",
                "properties": {
                    "sessionID": "ses_1",
                    "messageID": "msg_1",
                    "permissionID": "perm_1",
                    "question": "Allow bash?",
                    "status": "pending",
                },
            },
            "ses_1",
        )

        self.assertEqual(admission["session_id"], "ses_1")
        self.assertEqual(admission["message_id"], "msg_1")
        self.assertEqual(admission["delivery"], "queue")
        self.assertEqual(admission["status"], "active")
        self.assertEqual(event["kind"], "blocker")
        self.assertEqual(event["blocker"], "permission")
        self.assertEqual(event["blocker_id"], "perm_1")
        self.assertEqual(event["status"], "queued")


if __name__ == "__main__":
    unittest.main()
