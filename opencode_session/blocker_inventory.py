from copy import deepcopy

from opencode_session.schema_helpers import (
    CALL_ID_ALIASES,
    MESSAGE_ID_ALIASES,
    REQUEST_ID_ALIASES,
    SESSION_ID_ALIASES,
    collection_records,
    first_present,
    set_missing,
)


BLOCKER_CANONICAL_FIELDS = ("id", "session_id", "tool_message_id", "tool_call_id", "tool_ref")


def load_blocker_counts(client):
    permission_response = client.list_permissions_response()
    question_response = client.list_questions_response()
    counts = {}
    for permission in collection_blockers(permission_response.data, "permissions"):
        _increment_blocker_count(counts, blocker_session_id(permission), "permissions")
    for question in collection_blockers(question_response.data, "questions"):
        _increment_blocker_count(counts, blocker_session_id(question), "questions")
    return counts


def blocker_counts_for_session(counts, session_id):
    session_counts = counts.get(session_id, {})
    permissions = session_counts.get("permissions", 0)
    questions = session_counts.get("questions", 0)
    return {"permissions": permissions, "questions": questions, "total": permissions + questions}


def collection_blockers(collection, plural_name):
    return [normalize_blocker_record(blocker) for blocker in collection_records(collection, plural_name, "requests", "data")]


def normalize_blocker_record(blocker):
    if not isinstance(blocker, dict):
        return unknown_blocker_record(blocker)
    normalized = dict(blocker)
    normalized["schema_status"] = "known"
    set_missing(normalized, "id", first_present(blocker, "id", *REQUEST_ID_ALIASES))
    set_missing(normalized, "session_id", first_present(blocker, *SESSION_ID_ALIASES))
    _apply_tool_fields(normalized, blocker.get("tool"))
    return normalized


def blocker_session_id(blocker):
    if not isinstance(blocker, dict):
        return None
    return blocker.get("session_id")


def unknown_blocker_record(raw):
    normalized = {field_name: None for field_name in BLOCKER_CANONICAL_FIELDS}
    normalized["schema_status"] = "unknown"
    normalized["raw"] = deepcopy(raw)
    return normalized


def _apply_tool_fields(normalized, tool):
    tool_message_id = first_present(normalized, "tool_message_id")
    tool_call_id = first_present(normalized, "tool_call_id")
    if isinstance(tool, dict):
        tool_message_id = tool_message_id or first_present(tool, *MESSAGE_ID_ALIASES)
        tool_call_id = tool_call_id or first_present(tool, *CALL_ID_ALIASES)
    set_missing(normalized, "tool_message_id", tool_message_id)
    set_missing(normalized, "tool_call_id", tool_call_id)
    set_missing(normalized, "tool_ref", _tool_ref(tool_message_id, tool_call_id))


def _tool_ref(message_id, call_id):
    if message_id and call_id:
        return f"{message_id}/{call_id}"
    return call_id or message_id


def _increment_blocker_count(counts, session_id, name):
    if not session_id:
        return
    session_counts = counts.setdefault(session_id, {"permissions": 0, "questions": 0})
    session_counts[name] += 1
