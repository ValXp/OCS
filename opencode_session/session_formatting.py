from opencode_session.formatting import compact_value as _compact_value
from opencode_session.formatting import format_table as _format_table
from opencode_session.records import session_record as _session_record
from opencode_session.records import tokens_total as _tokens_total
from opencode_session.session_services import counts_for_session


def format_session_compact(session, blocker_counts=None):
    session = _session_record(session)
    fields = [
        ("id", session.get("id")),
        ("title", session.get("title")),
        ("dir", session.get("directory")),
        ("agent", session.get("agent")),
        ("model", session.get("model")),
        ("cost", session.get("cost")),
        ("tokens", session_tokens(session)),
        ("created", session.get("createdAt")),
        ("updated", session.get("updatedAt")),
    ]
    if blocker_counts is not None:
        fields.extend(
            [
                ("permissions", blocker_counts["permissions"]),
                ("questions", blocker_counts["questions"]),
                ("blockers", blocker_counts["total"]),
            ]
        )
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def format_session_table(sessions, blocker_counts=None):
    headers = ["id", "title", "dir", "agent", "model", "cost", "tokens", "updated"]
    if blocker_counts is not None:
        headers.extend(["permissions", "questions", "blockers"])
    rows = []
    for session in sessions:
        normalized = _session_record(session)
        row = [
            normalized.get("id"),
            normalized.get("title"),
            normalized.get("directory"),
            normalized.get("agent"),
            normalized.get("model"),
            normalized.get("cost"),
            session_tokens(normalized),
            normalized.get("updatedAt"),
        ]
        if blocker_counts is not None:
            counts = counts_for_session(blocker_counts, normalized)
            row.extend([counts["permissions"], counts["questions"], counts["total"]])
        rows.append(row)
    return _format_table(headers, rows)


def format_fork_compact(fork):
    fields = [
        ("parent", fork["parent_session_id"]),
        ("child", fork["session_id"]),
        ("message", fork["message_id"]),
    ]
    return "forked " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def session_tokens(session):
    session = _session_record(session)
    return _tokens_total(session.get("tokens"))
