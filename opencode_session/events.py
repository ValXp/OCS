import json

from opencode_session.formatting import compact_value as _compact_value
from opencode_session.schema_normalization import normalize_event_record
from opencode_session.status import short_status


class EventStreamError(Exception):
    pass


def iter_event_stream(lines):
    event_name = None
    event_id = None
    data_lines = []
    for raw_line in lines:
        line = _decode_line(raw_line).rstrip("\r\n")
        if line == "":
            event = _event_from_parts(event_name, event_id, data_lines)
            if event is not None:
                yield event
            event_name = None
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("{") and not data_lines:
            event = _event_from_parts(None, None, [line])
            if event is not None:
                yield event
            continue
        field, separator, value = line.partition(":")
        if not separator:
            data_lines.append(line)
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)

    event = _event_from_parts(event_name, event_id, data_lines)
    if event is not None:
        yield event


def normalize_event(event, target_session_id=None, *, route_path=None):
    normalized = normalize_event_record(event, target_session_id, route_path=route_path)
    if normalized.get("kind") == "ignored":
        return None
    return normalized


def format_watch_event(event):
    kind = event["kind"]
    fields = [("session", event.get("session_id"))]
    if kind == "admission":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("delivery", event.get("delivery")),
                ("status", event.get("status")),
            ]
        )
    elif kind == "tool":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("call", event.get("call_id")),
                ("tool", event.get("tool")),
                ("status", event.get("status")),
            ]
        )
    elif kind == "status":
        fields.append(("status", event.get("status")))
    elif kind == "prompt":
        fields.extend([("message", event.get("message_id")), ("status", event.get("status"))])
    elif kind == "step":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("step", event.get("step")),
                ("status", event.get("status")),
                ("title", event.get("title")),
            ]
        )
    elif kind == "text":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("chars", len(event.get("text") or "")),
                ("text", event.get("text")),
            ]
        )
    elif kind == "error":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("status", event.get("status")),
                ("error", event.get("error")),
            ]
        )
    elif kind == "blocker":
        fields.extend(
            [
                ("blocker", event.get("blocker")),
                ("id", event.get("blocker_id")),
                ("message", event.get("message_id")),
                ("question", event.get("question")),
            ]
        )
    else:
        fields.extend([("message", event.get("message_id")), ("status", event.get("status"))])
    return " ".join([kind, *[f"{name}={_compact_value(value)}" for name, value in fields if value is not None]])


def is_terminal_event(event):
    status = short_status(event.get("status"))
    if status in {"done", "aborted", "timeout"}:
        return True
    return status == "failed" and event.get("kind") == "status"


def is_abort_event(event):
    return short_status(event.get("status")) == "aborted"


def _event_from_parts(event_name, event_id, data_lines):
    if not data_lines and event_name is None and event_id is None:
        return None
    data_text = "\n".join(data_lines)
    data = _decode_data(data_text) if data_text else {}
    if isinstance(data, dict):
        event = dict(data)
    else:
        event = {"data": data}
    if event_name is not None:
        event.setdefault("event", event_name)
    if event_id is not None:
        event.setdefault("event_id", event_id)
    return event


def _decode_data(data_text):
    try:
        return json.loads(data_text)
    except json.JSONDecodeError as error:
        raise EventStreamError(f"invalid JSON: {error.msg}") from error


def _decode_line(raw_line):
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return str(raw_line)
