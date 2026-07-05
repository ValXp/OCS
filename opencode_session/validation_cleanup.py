import json
from pathlib import Path

from opencode_session.api_client import OpenCodeApiError
from opencode_session.disposable_session_lifecycle import delete_and_verify_disposable_session
from opencode_session.formatting import compact_value
from opencode_session.schema_session_adapter import collection_sessions, session_value


def cleanup_disposable_command(args, client, *, print_error, unavailable_exit):
    directory = str(Path(args.directory).resolve()) if args.directory else None
    try:
        response = client.list_sessions_response()
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

    sessions = [
        session
        for session in collection_sessions(response.data)
        if is_disposable_session(session, prefix=args.prefix, directory=directory)
    ]
    result = {
        "status": "done",
        "prefix": args.prefix,
        "directory": directory,
        "stale": len(sessions),
        "sessions": [session_value(session, "id", "sessionID", "sessionId") for session in sessions],
        "deleted": [],
        "verified": [],
        "errors": [],
    }
    for session in sessions:
        session_id = session_value(session, "id", "sessionID", "sessionId")
        if not session_id:
            result["status"] = "failed"
            result["errors"].append({"session_id": None, "error": "session has no id"})
            continue
        error = delete_and_verify_disposable_session(client, session_id)
        if error is not None:
            result["status"] = "failed"
            result["errors"].append({"session_id": session_id, "error": str(error)})
            continue
        result["deleted"].append(session_id)
        result["verified"].append(session_id)

    if result["status"] != "done":
        print_error(f"cleanup failed: {format_cleanup_command_compact(result)}")
        return unavailable_exit
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(format_cleanup_command_compact(result))
    return 0


def is_disposable_session(session, *, prefix, directory):
    if directory is not None and session_value(session, "directory", "cwd") != directory:
        return False
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    values = [
        session_value(session, "id", "sessionID", "sessionId"),
        session_value(session, "title", "name"),
        metadata.get("smoke_id"),
        metadata.get("prefix"),
        metadata.get("disposable_prefix"),
    ]
    return any(str(value).startswith(prefix) for value in values if value is not None)


def format_cleanup_summary(cleanup):
    return " ".join(
        [
            f"cleanup={cleanup.get('status')}",
            f"deleted={len(cleanup.get('deleted') or [])}",
            f"verified={len(cleanup.get('verified') or [])}",
        ]
    )


def format_cleanup_command_compact(result):
    fields = [
        ("stale", result["stale"]),
        ("deleted", len(result["deleted"])),
        ("verified", len(result["verified"])),
        ("prefix", result["prefix"]),
        ("dir", result["directory"]),
    ]
    return "cleanup " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)
