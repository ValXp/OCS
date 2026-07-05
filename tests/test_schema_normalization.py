import unittest

from opencode_session.events import normalize_event
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
        self.assertEqual(session["schema_status"], "known")
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
        self.assertEqual(event["schema_status"], "known")
        self.assertEqual(event["blocker"], "permission")
        self.assertEqual(event["blocker_id"], "perm_1")
        self.assertEqual(event["status"], "queued")

    def test_unknown_session_shapes_are_explicit_records(self):
        self.assertEqual(
            normalize_session_payload("not-a-session-record"),
            {
                "schema_status": "unknown",
                "id": None,
                "directory": None,
                "title": None,
                "agent": None,
                "model": None,
                "tokens": None,
                "createdAt": None,
                "updatedAt": None,
                "raw": "not-a-session-record",
            },
        )
        self.assertEqual(
            normalize_session_payload({"unexpected": True}),
            {
                "schema_status": "unknown",
                "id": None,
                "directory": None,
                "title": None,
                "agent": None,
                "model": None,
                "tokens": None,
                "createdAt": None,
                "updatedAt": None,
                "raw": {"unexpected": True},
            },
        )

    def test_event_session_mismatch_is_explicit_but_watcher_boundary_filters_it(self):
        event = {
            "type": "session.status",
            "properties": {"sessionID": "ses_other", "status": "completed"},
        }

        normalized = normalize_event_record(event, "ses_target")

        self.assertEqual(
            normalized,
            {
                "kind": "ignored",
                "schema_status": "known",
                "target_session_id": "ses_target",
                "reason": "session_mismatch",
                "session_id": "ses_other",
                "type": "session.status",
            },
        )
        self.assertIsNone(normalize_event(event, "ses_target"))

    def test_unknown_event_shapes_are_not_classified_by_substrings(self):
        event = {
            "type": "session.custom.statusish",
            "properties": {"sessionID": "ses_target", "messageID": "msg_1", "status": "completed"},
        }

        normalized = normalize_event_record(event, "ses_target")

        self.assertEqual(normalized["kind"], "unknown")
        self.assertEqual(normalized["schema_status"], "unknown")
        self.assertEqual(normalized["reason"], "unrecognized_event_shape")
        self.assertEqual(normalized["session_id"], "ses_target")
        self.assertEqual(normalized["type"], "session.custom.statusish")
        self.assertEqual(normalized["raw"], event)
        self.assertNotIn("status", normalized)


if __name__ == "__main__":
    unittest.main()
