from opencode_session.formatting import (
    compact_bool as _compact_bool,
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
)


def format_permission_compact(permission):
    fields = [
        ("id", permission.get("id")),
        ("session", permission.get("session_id")),
        ("permission", permission.get("permission")),
        ("patterns", _compact_list(permission.get("patterns"))),
        ("always", _compact_list(permission.get("always"))),
        ("tool", permission.get("tool_ref")),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_permission_table(permissions):
    rows = []
    for permission in permissions:
        rows.append(
            [
                permission.get("id"),
                permission.get("session_id"),
                permission.get("permission"),
                _compact_list(permission.get("patterns")),
                _compact_list(permission.get("always")),
                permission.get("tool_ref"),
            ]
        )
    return _format_table(["id", "session", "permission", "patterns", "always", "tool"], rows)


def format_permission_reply_compact(result):
    fields = [("id", result["id"]), ("reply", result["reply"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_question_compact(question):
    question_items = _question_items(question)
    fields = [
        ("id", question.get("id")),
        ("session", question.get("session_id")),
        ("questions", len(question_items)),
        ("headers", _compact_list(item.get("header") for item in question_items if isinstance(item, dict))),
        ("question", _first_question_text(question_items)),
        ("tool", question.get("tool_ref")),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_question_table(questions):
    rows = []
    for question in questions:
        question_items = _question_items(question)
        rows.append(
            [
                question.get("id"),
                question.get("session_id"),
                len(question_items),
                _compact_list(item.get("header") for item in question_items if isinstance(item, dict)),
                _first_question_text(question_items),
                question.get("tool_ref"),
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
