from opencode_session.blocker_inventory import blocker_session_id as _blocker_session_id
from opencode_session.formatting import (
    compact_bool as _compact_bool,
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
)
from opencode_session.schema_common import first_present as _first_present


def format_permission_compact(permission):
    fields = [
        ("id", _first_present(permission, "id", "requestID", "requestId")),
        ("session", _blocker_session_id(permission)),
        ("permission", permission.get("permission")),
        ("patterns", _compact_list(permission.get("patterns"))),
        ("always", _compact_list(permission.get("always"))),
        ("tool", _tool_ref(permission.get("tool"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_permission_table(permissions):
    rows = []
    for permission in permissions:
        rows.append(
            [
                _first_present(permission, "id", "requestID", "requestId"),
                _blocker_session_id(permission),
                permission.get("permission"),
                _compact_list(permission.get("patterns")),
                _compact_list(permission.get("always")),
                _tool_ref(permission.get("tool")),
            ]
        )
    return _format_table(["id", "session", "permission", "patterns", "always", "tool"], rows)


def format_permission_reply_compact(result):
    fields = [("id", result["id"]), ("reply", result["reply"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_question_compact(question):
    question_items = _question_items(question)
    fields = [
        ("id", _first_present(question, "id", "requestID", "requestId")),
        ("session", _blocker_session_id(question)),
        ("questions", len(question_items)),
        ("headers", _compact_list(item.get("header") for item in question_items if isinstance(item, dict))),
        ("question", _first_question_text(question_items)),
        ("tool", _tool_ref(question.get("tool"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_question_table(questions):
    rows = []
    for question in questions:
        question_items = _question_items(question)
        rows.append(
            [
                _first_present(question, "id", "requestID", "requestId"),
                _blocker_session_id(question),
                len(question_items),
                _compact_list(item.get("header") for item in question_items if isinstance(item, dict)),
                _first_question_text(question_items),
                _tool_ref(question.get("tool")),
            ]
        )
    return _format_table(["id", "session", "questions", "headers", "question", "tool"], rows)


def format_question_resolution_compact(result):
    fields = [("id", result["id"]), ("action", result["action"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _question_items(question):
    items = question.get("questions")
    return items if isinstance(items, list) else []


def _first_question_text(question_items):
    for item in question_items:
        if isinstance(item, dict) and item.get("question"):
            return item.get("question")
    return None


def _tool_ref(tool):
    if not isinstance(tool, dict):
        return None
    message_id = _first_present(tool, "messageID", "messageId", "message_id")
    call_id = _first_present(tool, "callID", "callId", "call_id")
    if message_id and call_id:
        return f"{message_id}/{call_id}"
    return call_id or message_id
