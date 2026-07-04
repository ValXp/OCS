from opencode_session.formatting import compact_bool, compact_value
from opencode_session.records import bool_value, first_present
from opencode_session.status import short_status


def is_session_not_found_error(error):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    parts = path.split("/")
    if method == "POST" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] in {"run", "reply", "message", "abort", "fork"}
    if method == "GET" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] == "children"
    return method in {"GET", "DELETE"} and len(parts) == 4 and parts[1:3] == ["api", "session"] and bool(parts[3])


def abort_record(session_id, data):
    if not isinstance(data, dict):
        data = {}
    raw_status = first_present(data, "status", "state")
    accepted = bool_value(first_present(data, "accepted", "aborted", "ok", "success"))
    if accepted is None and str(raw_status or "").lower() in {"accepted", "aborting", "abort", "aborted", "cancelled", "canceled"}:
        accepted = True
    return {
        "session_id": first_present(data, "sessionID", "sessionId", "session_id", "id") or session_id,
        "accepted": accepted if accepted is not None else True,
        "status": short_status(raw_status),
        "raw_status": raw_status,
        "response": data,
    }


def format_abort_compact(abort):
    fields = [
        ("session", abort["session_id"]),
        ("accepted", compact_bool(abort["accepted"])),
        ("status", abort["status"]),
    ]
    return "abort " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)
