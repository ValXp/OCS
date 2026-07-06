from copy import deepcopy

from opencode_session.schema_common import (
    NormalizedSessionRecord,
    collection_records,
    first_present,
    normalized_tokens,
    set_missing,
)


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")


def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    normalize_record = _session_normalizer(route_path, route_plan)
    return _normalize_session_payload(payload, normalize_record)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return _session_normalizer(route_path, route_plan)(record)


def _normalize_session_payload(payload, normalize_record):
    if isinstance(payload, list):
        return [normalize_record(item) for item in payload]
    if not isinstance(payload, dict):
        return unknown_session_record(payload)

    normalized = dict(payload)
    data = normalized.get("data")
    if isinstance(data, list):
        normalized["data"] = [normalize_record(item) for item in data]
        return normalized
    if isinstance(data, dict):
        normalized["data"] = normalize_record(data)
        return normalized

    for name in ("sessions", "children"):
        records = normalized.get(name)
        if isinstance(records, list):
            normalized[name] = [normalize_record(item) for item in records]
            return normalized

    return normalize_record(normalized)


def _session_normalizer(route_path=None, route_plan=None):
    path = route_path
    if path is None and isinstance(route_plan, dict):
        path = route_plan.get("session_collection")
    normalized_path = str(path or "").split("?", 1)[0].rstrip("/")
    if normalized_path == "/api/session":
        return _normalize_api_session_record
    if normalized_path == "/session":
        return _normalize_compatible_session_record
    return _normalize_compatible_session_record


def _normalize_compatible_session_record(record) -> NormalizedSessionRecord:
    if not isinstance(record, dict):
        return unknown_session_record(record)
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = _normalize_compatible_session_record(record["data"])
        return normalized

    fields = _compatible_session_fields(record)
    if not _has_any_field(fields):
        return unknown_session_record(record)

    normalized = dict(record)
    normalized["schema_status"] = "known"
    _apply_session_fields(normalized, fields)
    return normalized


def _normalize_api_session_record(record) -> NormalizedSessionRecord:
    if not isinstance(record, dict):
        return unknown_session_record(record)
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = _normalize_api_session_record(record["data"])
        return normalized

    fields = _api_session_fields(record)
    if not _has_any_field(fields):
        return unknown_session_record(record)

    normalized = dict(record)
    normalized["schema_status"] = "known"
    _apply_session_fields(normalized, fields)
    return normalized


def _apply_session_fields(normalized, fields):
    set_missing(normalized, "id", fields["id"])
    set_missing(normalized, "directory", fields["directory"])
    set_missing(normalized, "title", fields["title"])
    set_missing(normalized, "agent", fields["agent"])
    set_missing(normalized, "model", fields["model"])
    set_missing(normalized, "tokens", normalized_tokens(fields["tokens"]))
    set_missing(normalized, "createdAt", fields["createdAt"])
    set_missing(normalized, "updatedAt", fields["updatedAt"])
    require_session_canonical_fields(normalized)


def _compatible_session_fields(record):
    return {
        "id": _compatible_session_id(record),
        "directory": _compatible_session_directory(record),
        "title": _compatible_session_title(record),
        "agent": _compatible_session_agent(record),
        "model": _compatible_session_model(record),
        "tokens": _compatible_session_tokens(record),
        "createdAt": _compatible_session_created_at(record),
        "updatedAt": _compatible_session_updated_at(record),
    }


def _api_session_fields(record):
    return {
        "id": _api_session_id(record),
        "directory": _api_session_directory(record),
        "title": _api_session_title(record),
        "agent": _api_session_agent(record),
        "model": _api_session_model(record),
        "tokens": _api_session_tokens(record),
        "createdAt": _api_session_created_at(record),
        "updatedAt": _api_session_updated_at(record),
    }


def _compatible_session_id(record):
    return _root_or_info_value(record, "id", "sessionID", "sessionId", "session_id")


def _compatible_session_directory(record):
    return _first_not_none(
        _root_or_info_value(record, "directory", "cwd"),
        _child_value(record, "location", "directory"),
    )


def _compatible_session_title(record):
    return _root_or_info_value(record, "title", "name")


def _compatible_session_agent(record):
    return _root_or_info_value(record, "agent", "agentID", "agentId", "agent_id")


def _compatible_session_model(record):
    return _root_or_info_value(record, "model", "modelID", "modelId", "model_id")


def _compatible_session_tokens(record):
    return _root_or_info_value(record, "tokens", "token", "tokenUsage", "token_usage", "usage")


def _compatible_session_created_at(record):
    return _first_not_none(
        _root_or_info_value(record, "createdAt", "created_at", "created"),
        _child_value(record, "time", "created"),
    )


def _compatible_session_updated_at(record):
    return _first_not_none(
        _root_or_info_value(record, "updatedAt", "updated_at", "updated"),
        _child_value(record, "time", "updated"),
    )


def _api_session_id(record):
    return _root_or_info_value(record, "id")


def _api_session_directory(record):
    return _first_not_none(
        _root_or_info_value(record, "directory", "cwd"),
        _child_value(record, "location", "directory"),
    )


def _api_session_title(record):
    return _root_or_info_value(record, "title")


def _api_session_agent(record):
    return _root_or_info_value(record, "agent")


def _api_session_model(record):
    return _root_or_info_value(record, "model")


def _api_session_tokens(record):
    return _root_or_info_value(record, "tokens", "tokenUsage", "usage")


def _api_session_created_at(record):
    return _first_not_none(
        _root_or_info_value(record, "createdAt", "created"),
        _child_value(record, "time", "created"),
    )


def _api_session_updated_at(record):
    return _first_not_none(
        _root_or_info_value(record, "updatedAt", "updated"),
        _child_value(record, "time", "updated"),
    )


def _root_or_info_value(record, *names):
    value = first_present(record, *names)
    if value is not None:
        return value
    info = record.get("info") if isinstance(record, dict) else None
    return first_present(info, *names)


def _child_value(record, child_name, *names):
    child = record.get(child_name) if isinstance(record, dict) else None
    return first_present(child, *names)


def _first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _has_any_field(fields):
    return any(value is not None for value in fields.values())


def unknown_session_record(raw) -> NormalizedSessionRecord:
    normalized = {field_name: None for field_name in SESSION_CANONICAL_FIELDS}
    normalized["schema_status"] = "unknown"
    normalized["raw"] = deepcopy(raw)
    return normalized


def require_session_canonical_fields(record):
    for field_name in SESSION_CANONICAL_FIELDS:
        record.setdefault(field_name, None)


def collection_sessions(collection):
    return collection_records(collection, "sessions", "children", "data")


def session_record(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        return session["data"]
    return session if isinstance(session, dict) else {}


def session_value(session, *names):
    record = session_record(session)
    if _requested(names, "id", "sessionID", "sessionId", "session_id"):
        value = _compatible_session_id(record)
        if value is not None:
            return value
    if _requested(names, "directory", "cwd"):
        value = _compatible_session_directory(record)
        if value is not None:
            return value
    if _requested(names, "title", "name"):
        value = _compatible_session_title(record)
        if value is not None:
            return value
    if _requested(names, "agent", "agentID", "agentId", "agent_id"):
        value = _compatible_session_agent(record)
        if value is not None:
            return value
    if _requested(names, "model", "modelID", "modelId", "model_id"):
        value = _compatible_session_model(record)
        if value is not None:
            return value
    if _requested(names, "tokens", "token", "tokenUsage", "token_usage", "usage"):
        value = _compatible_session_tokens(record)
        if value is not None:
            return value
    if _requested(names, "createdAt", "created_at", "created"):
        value = _compatible_session_created_at(record)
        if value is not None:
            return value
    if _requested(names, "updatedAt", "updated_at", "updated"):
        value = _compatible_session_updated_at(record)
        if value is not None:
            return value
    return None


def _requested(requested_names, *aliases):
    return any(name in aliases for name in requested_names)
