from copy import deepcopy

from opencode_session.schema_common import (
    AGENT_ID_ALIASES,
    API_CREATED_AT_ALIASES,
    API_TOKEN_ALIASES,
    API_UPDATED_AT_ALIASES,
    CREATED_AT_ALIASES,
    MODEL_ID_ALIASES,
    NormalizedSessionRecord,
    SESSION_ID_ALIASES,
    TOKEN_ALIASES,
    UPDATED_AT_ALIASES,
    child_value,
    collection_records,
    first_not_none,
    normalized_tokens,
    root_or_info_value,
    set_missing,
)


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")
SESSION_VALUE_ALIASES = (
    ("id", ("id", *SESSION_ID_ALIASES)),
    ("directory", ("directory", "cwd")),
    ("title", ("title", "name")),
    ("agent", ("agent", *AGENT_ID_ALIASES)),
    ("model", ("model", *MODEL_ID_ALIASES)),
    ("tokens", TOKEN_ALIASES),
    ("createdAt", CREATED_AT_ALIASES),
    ("updatedAt", UPDATED_AT_ALIASES),
)


def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    return _session_payload_normalizer(route_path, route_plan)(payload)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return _session_record_normalizer(route_path, route_plan)(record)


def _normalize_api_session_payload(payload):
    return _normalize_session_payload(payload, _normalize_api_session_record, ("data", "sessions"))


def _normalize_legacy_session_payload(payload):
    return _normalize_session_payload(payload, _normalize_legacy_session_record, ("sessions", "children", "data"))


def _normalize_session_payload(payload, normalize_record, collection_names):
    if isinstance(payload, list):
        return [normalize_record(item) for item in payload]
    if not isinstance(payload, dict):
        return unknown_session_record(payload)

    normalized = dict(payload)
    for name in collection_names:
        records = normalized.get(name)
        if isinstance(records, list):
            normalized[name] = [normalize_record(item) for item in records]
            return normalized
        if name == "data" and isinstance(records, dict):
            normalized[name] = normalize_record(records)
            return normalized

    return normalize_record(normalized)


def _session_payload_normalizer(route_path=None, route_plan=None):
    path = route_path
    if path is None and isinstance(route_plan, dict):
        path = route_plan.get("session_collection")
    normalized_path = str(path or "").split("?", 1)[0].rstrip("/")
    if normalized_path == "/api/session":
        return _normalize_api_session_payload
    if normalized_path == "/session":
        return _normalize_legacy_session_payload
    return _normalize_legacy_session_payload


def _session_record_normalizer(route_path=None, route_plan=None):
    path = route_path
    if path is None and isinstance(route_plan, dict):
        path = route_plan.get("session_collection")
    normalized_path = str(path or "").split("?", 1)[0].rstrip("/")
    if normalized_path == "/api/session":
        return _normalize_api_session_record
    if normalized_path == "/session":
        return _normalize_legacy_session_record
    return _normalize_legacy_session_record


def _normalize_legacy_session_record(record) -> NormalizedSessionRecord:
    if not isinstance(record, dict):
        return unknown_session_record(record)
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = _normalize_legacy_session_record(record["data"])
        return normalized

    fields = _legacy_session_fields(record)
    if not _has_session_identity(fields):
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
    if not _has_session_identity(fields):
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


def _legacy_session_fields(record):
    return {
        "id": root_or_info_value(record, "id", *SESSION_ID_ALIASES),
        "directory": first_not_none(
            root_or_info_value(record, "directory", "cwd"),
            child_value(record, "location", "directory"),
        ),
        "title": root_or_info_value(record, "title", "name"),
        "agent": root_or_info_value(record, "agent", *AGENT_ID_ALIASES),
        "model": root_or_info_value(record, "model", *MODEL_ID_ALIASES),
        "tokens": root_or_info_value(record, *TOKEN_ALIASES),
        "createdAt": first_not_none(
            root_or_info_value(record, *CREATED_AT_ALIASES),
            child_value(record, "time", "created"),
        ),
        "updatedAt": first_not_none(
            root_or_info_value(record, *UPDATED_AT_ALIASES),
            child_value(record, "time", "updated"),
        ),
    }


def _api_session_fields(record):
    return {
        "id": root_or_info_value(record, "id"),
        "directory": first_not_none(
            root_or_info_value(record, "directory", "cwd"),
            child_value(record, "location", "directory"),
        ),
        "title": root_or_info_value(record, "title"),
        "agent": root_or_info_value(record, "agent"),
        "model": root_or_info_value(record, "model"),
        "tokens": root_or_info_value(record, *API_TOKEN_ALIASES),
        "createdAt": first_not_none(
            root_or_info_value(record, *API_CREATED_AT_ALIASES),
            child_value(record, "time", "created"),
        ),
        "updatedAt": first_not_none(
            root_or_info_value(record, *API_UPDATED_AT_ALIASES),
            child_value(record, "time", "updated"),
        ),
    }


def _has_session_identity(fields):
    return fields["id"] is not None


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


def session_value(session, *names, route_path=None, route_plan=None):
    normalized = normalize_session_record(session_record(session), route_path=route_path, route_plan=route_plan)
    if normalized.get("schema_status") == "unknown":
        return None
    for field_name, aliases in SESSION_VALUE_ALIASES:
        if _requested(names, *aliases):
            return normalized.get(field_name)
    return None


def _requested(requested_names, *aliases):
    return any(name in aliases for name in requested_names)
