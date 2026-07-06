import unittest

from opencode_session.events import normalize_event
from opencode_session.schema_admission_adapter import normalize_admission_record
from opencode_session.schema_common import NormalizedEventRecord, PersistedRunRecord, WorkerSnapshotRecord
from opencode_session.schema_event_adapter import normalize_event_record
from opencode_session.schema_message_adapter import (
    LEGACY_MESSAGE_ROUTE,
    SESSION_MESSAGE_ROUTE,
    iter_normalized_message_records,
    message_value,
    normalize_message_record,
)
from opencode_session.schema_session_adapter import normalize_session_payload, session_value


KNOWN_EVENT_ROUTE_FIXTURES = (
    (
        "/api/event",
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
        {
            "kind": "blocker",
            "schema_status": "known",
            "session_id": "ses_1",
            "type": "permission.requested",
            "message_id": "msg_1",
            "status": "queued",
            "raw_status": "pending",
            "blocker": "permission",
            "blocker_id": "perm_1",
            "question": "Allow bash?",
        },
    ),
    (
        "/event",
        {
            "event": "session.status",
            "payload": {"sessionID": "ses_1", "status": "completed"},
        },
        {
            "kind": "status",
            "schema_status": "known",
            "session_id": "ses_1",
            "type": "session.status",
            "status": "done",
            "raw_status": "completed",
        },
    ),
    (
        "/api/event",
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_1",
                "messageID": "msg_assistant",
                "message": {"role": "assistant", "status": "completed", "text": "PONG"},
            },
        },
        {
            "kind": "text",
            "schema_status": "known",
            "session_id": "ses_1",
            "type": "message.part.updated",
            "message_id": "msg_assistant",
            "status": "done",
            "raw_status": "completed",
            "text": "PONG",
        },
    ),
)


class SchemaNormalizationTest(unittest.TestCase):
    def test_sparse_schema_boundaries_are_marked_total_false(self):
        self.assertFalse(NormalizedEventRecord.__total__)
        self.assertFalse(PersistedRunRecord.__total__)
        self.assertFalse(WorkerSnapshotRecord.__total__)

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

    def test_session_route_path_selects_api_boundary_normalization(self):
        payload = {"sessions": [{"sessionID": "ses_legacy", "name": "Legacy"}]}

        api_session = normalize_session_payload(payload, route_path="/api/session")["sessions"][0]
        legacy_session = normalize_session_payload(payload, route_path="/session")["sessions"][0]

        self.assertEqual(api_session["schema_status"], "unknown")
        self.assertEqual(legacy_session["schema_status"], "known")
        self.assertEqual(legacy_session["id"], "ses_legacy")

    def test_api_session_route_normalizes_explicit_api_shape(self):
        payload = {
            "data": [
                {
                    "id": "ses_api",
                    "title": "API session",
                    "directory": "/tmp/project",
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                    "usage": {"input": 4, "output": 6},
                    "created": "2026-07-01T00:00:00Z",
                    "updated": "2026-07-01T00:00:03Z",
                }
            ]
        }

        session = normalize_session_payload(payload, route_path="/api/session")["data"][0]

        self.assertEqual(session["schema_status"], "known")
        self.assertEqual(session["id"], "ses_api")
        self.assertEqual(session["title"], "API session")
        self.assertEqual(session["directory"], "/tmp/project")
        self.assertEqual(session["agent"], "build")
        self.assertEqual(session["model"], "openai/gpt-5.5")
        self.assertEqual(session["tokens"], {"input": 4, "output": 6, "total": 10})
        self.assertEqual(session["createdAt"], "2026-07-01T00:00:00Z")
        self.assertEqual(session["updatedAt"], "2026-07-01T00:00:03Z")

    def test_session_routes_require_record_identity_before_known_normalization(self):
        api_payload = {"data": [{"title": "Missing id"}]}
        legacy_payload = {"children": [{"name": "Missing id"}]}

        api_session = normalize_session_payload(api_payload, route_path="/api/session")["data"][0]
        legacy_session = normalize_session_payload(legacy_payload, route_path="/session")["children"][0]

        self.assertEqual(api_session["schema_status"], "unknown")
        self.assertEqual(api_session["raw"], {"title": "Missing id"})
        self.assertEqual(legacy_session["schema_status"], "unknown")
        self.assertEqual(legacy_session["raw"], {"name": "Missing id"})

    def test_session_value_preserves_wrapped_record_compatibility(self):
        session = {"data": {"sessionID": "ses_wrapped", "name": "Wrapped"}}

        self.assertEqual(session_value(session, "id", "sessionID", "sessionId"), "ses_wrapped")
        self.assertEqual(session_value(session, "title", "name"), "Wrapped")

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

    def test_known_event_route_fixtures_decode_explicit_shapes(self):
        for route_path, raw_event, expected in KNOWN_EVENT_ROUTE_FIXTURES:
            with self.subTest(route_path=route_path):
                self.assertEqual(normalize_event_record(raw_event, "ses_1", route_path=route_path), expected)

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

    def test_unknown_message_shapes_are_explicit_records(self):
        self.assertEqual(
            normalize_message_record({"unexpected": True}),
            {
                "schema_status": "unknown",
                "id": None,
                "role": None,
                "status": None,
                "raw_status": None,
                "cost": None,
                "tokens": None,
                "text": "",
                "raw": {"unexpected": True},
            },
        )

    def test_message_error_only_shape_is_known_for_provider_failures(self):
        message = {"data": {"reason": "quota exceeded"}}

        self.assertEqual(
            normalize_message_record(message),
            {
                "reason": "quota exceeded",
                "id": None,
                "role": None,
                "status": None,
                "raw_status": None,
                "cost": None,
                "tokens": None,
                "text": "",
            },
        )
        self.assertEqual(message_value(message, "error", "reason", "message"), "quota exceeded")

    def test_message_route_adapters_accept_route_specific_shapes(self):
        session_message = {
            "info": {
                "id": "msg_modern",
                "role": "assistant",
                "tokens": {"input": 2, "output": 3},
            },
            "parts": [{"type": "text", "text": "PONG"}],
        }
        legacy_message = {
            "messageID": "msg_legacy",
            "author": "assistant",
            "state": "completed",
            "tokenUsage": {"input": 1, "output": 1},
            "content": "done",
        }

        normalized_session_message = normalize_message_record(session_message, route=SESSION_MESSAGE_ROUTE)
        normalized_legacy_message = normalize_message_record(legacy_message, route=LEGACY_MESSAGE_ROUTE)

        self.assertEqual(normalized_session_message["id"], "msg_modern")
        self.assertEqual(normalized_session_message["role"], "assistant")
        self.assertEqual(normalized_session_message["tokens"], {"input": 2, "output": 3, "total": 5})
        self.assertEqual(normalized_session_message["text"], "PONG")
        self.assertEqual(normalized_legacy_message["id"], "msg_legacy")
        self.assertEqual(normalized_legacy_message["role"], "assistant")
        self.assertEqual(normalized_legacy_message["status"], "done")
        self.assertEqual(normalized_legacy_message["raw_status"], "completed")
        self.assertEqual(normalized_legacy_message["tokens"], {"input": 1, "output": 1, "total": 2})

    def test_session_message_route_rejects_legacy_only_message_aliases(self):
        message = {"message_id": "msg_legacy", "kind": "assistant", "token_usage": {"input": 1}}

        normalized = normalize_message_record(message, route=SESSION_MESSAGE_ROUTE)

        self.assertEqual(normalized["schema_status"], "unknown")
        self.assertEqual(normalized["raw"], message)
        self.assertIsNone(message_value(message, "id", "messageID", "messageId", route=SESSION_MESSAGE_ROUTE))
        self.assertEqual(message_value(message, "id", "message_id"), "msg_legacy")

    def test_message_token_only_shape_is_unknown(self):
        message = {"tokenUsage": {"input": 1, "output": 2}}

        normalized = normalize_message_record(message, route=LEGACY_MESSAGE_ROUTE)

        self.assertEqual(normalized["schema_status"], "unknown")
        self.assertEqual(normalized["raw"], message)

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

    def test_event_route_adapter_is_selected_from_route_path(self):
        event = {
            "event": "session.status",
            "payload": {"sessionID": "ses_1", "status": "completed"},
        }

        api_event = normalize_event_record(event, "ses_1", route_path="/api/event")
        legacy_event = normalize_event_record(event, "ses_1", route_path="/event")

        self.assertEqual(api_event["kind"], "unknown")
        self.assertEqual(api_event["schema_status"], "unknown")
        self.assertEqual(api_event["raw"], event)
        self.assertEqual(legacy_event["kind"], "status")
        self.assertEqual(legacy_event["session_id"], "ses_1")
        self.assertEqual(legacy_event["status"], "done")

    def test_legacy_event_route_rejects_api_event_envelope(self):
        event = {
            "type": "session.status",
            "properties": {"sessionID": "ses_1", "status": "completed"},
        }

        legacy_event = normalize_event_record(event, "ses_1", route_path="/event")

        self.assertEqual(legacy_event["kind"], "unknown")
        self.assertEqual(legacy_event["schema_status"], "unknown")
        self.assertEqual(legacy_event["raw"], event)

    def test_known_event_type_without_route_payload_details_is_unknown(self):
        event = {"type": "session.status", "properties": {"unexpected": True}}

        normalized = normalize_event_record(event, route_path="/api/event")

        self.assertEqual(normalized["kind"], "unknown")
        self.assertEqual(normalized["schema_status"], "unknown")
        self.assertEqual(normalized["reason"], "unrecognized_event_shape")
        self.assertEqual(normalized["type"], "session.status")
        self.assertEqual(normalized["raw"], event)
        self.assertNotIn("status", normalized)

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
