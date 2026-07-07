from pathlib import Path

from opencode_session.capabilities import capabilities_from_openapi_doc, configure_client_route_plan
from opencode_session.disposable_session_lifecycle import delete_and_verify_disposable_session
from opencode_session.schema_session_adapter import collection_sessions, session_value


def cleanup_stale_disposable_sessions(client, *, prefix, directory=None):
    directory = str(Path(directory).resolve()) if directory else None
    configure_client_route_plan(client, capabilities_from_openapi_doc(client.get_openapi_doc()))
    response = client.list_sessions_response()

    sessions = [
        session
        for session in collection_sessions(response.data)
        if is_disposable_session(session, prefix=prefix, directory=directory)
    ]
    result = {
        "status": "done",
        "prefix": prefix,
        "directory": directory,
        "stale": len(sessions),
        "sessions": [session_value(session, "id") for session in sessions],
        "deleted": [],
        "verified": [],
        "errors": [],
    }
    for session in sessions:
        session_id = session_value(session, "id")
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

    return result


def is_disposable_session(session, *, prefix, directory):
    if directory is not None and session_value(session, "directory") != directory:
        return False
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    values = [
        session_value(session, "id"),
        session_value(session, "title"),
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
