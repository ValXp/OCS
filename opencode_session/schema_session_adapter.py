from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from opencode_session.schema_common import (
    NormalizedSessionRecord,
    child_value,
    collection_records,
    first_not_none,
    normalized_tokens,
    root_or_info_value,
    set_missing,
)


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")
API_SESSION_ROUTE = "/api/session"
LEGACY_SESSION_ROUTE = "/session"

LEGACY_SESSION_ID_FIELDS = ("id", "sessionID", "sessionId", "session_id")
LEGACY_SESSION_DIRECTORY_FIELDS = ("directory", "cwd")
LEGACY_SESSION_TITLE_FIELDS = ("title", "name")
LEGACY_SESSION_AGENT_FIELDS = ("agent", "agentID", "agentId", "agent_id")
LEGACY_SESSION_MODEL_FIELDS = ("model", "modelID", "modelId", "model_id")
LEGACY_SESSION_TOKEN_FIELDS = ("tokens", "token", "tokenUsage", "token_usage", "usage")
LEGACY_SESSION_CREATED_AT_FIELDS = ("createdAt", "created_at", "created")
LEGACY_SESSION_UPDATED_AT_FIELDS = ("updatedAt", "updated_at", "updated")

API_SESSION_ID_FIELDS = ("id",)
API_SESSION_DIRECTORY_FIELDS = ("directory", "cwd")
API_SESSION_TITLE_FIELDS = ("title",)
API_SESSION_AGENT_FIELDS = ("agent",)
API_SESSION_MODEL_FIELDS = ("model",)
API_SESSION_TOKEN_FIELDS = ("tokens", "tokenUsage", "usage")
API_SESSION_CREATED_AT_FIELDS = ("createdAt", "created")
API_SESSION_UPDATED_AT_FIELDS = ("updatedAt", "updated")

SESSION_VALUE_ALIASES = (
    ("id", LEGACY_SESSION_ID_FIELDS),
    ("directory", LEGACY_SESSION_DIRECTORY_FIELDS),
    ("title", LEGACY_SESSION_TITLE_FIELDS),
    ("agent", LEGACY_SESSION_AGENT_FIELDS),
    ("model", LEGACY_SESSION_MODEL_FIELDS),
    ("tokens", LEGACY_SESSION_TOKEN_FIELDS),
    ("createdAt", LEGACY_SESSION_CREATED_AT_FIELDS),
    ("updatedAt", LEGACY_SESSION_UPDATED_AT_FIELDS),
)


@dataclass(frozen=True)
class SessionRouteAdapter:
    route: str
    version: str
    normalize_payload: Callable
    normalize_record: Callable


def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_payload(payload)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_record(record)


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


def _normalize_unknown_session_payload(payload):
    return unknown_session_record(payload)


def session_adapter_for_route(route_path=None, route_plan=None):
    route_key = _session_route_key(route_path, route_plan)
    if route_key is None:
        return DEFAULT_SESSION_ADAPTER
    return SESSION_ROUTE_ADAPTERS.get(route_key, UNKNOWN_SESSION_ADAPTER)


def _session_route_key(route_path=None, route_plan=None):
    path = route_path
    if path is None and isinstance(route_plan, dict):
        path = route_plan.get("session_collection")
    if path is None:
        return None
    return str(path).split("?", 1)[0].rstrip("/")


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
        "id": root_or_info_value(record, *LEGACY_SESSION_ID_FIELDS),
        "directory": first_not_none(
            root_or_info_value(record, *LEGACY_SESSION_DIRECTORY_FIELDS),
            child_value(record, "location", "directory"),
        ),
        "title": root_or_info_value(record, *LEGACY_SESSION_TITLE_FIELDS),
        "agent": root_or_info_value(record, *LEGACY_SESSION_AGENT_FIELDS),
        "model": root_or_info_value(record, *LEGACY_SESSION_MODEL_FIELDS),
        "tokens": root_or_info_value(record, *LEGACY_SESSION_TOKEN_FIELDS),
        "createdAt": first_not_none(
            root_or_info_value(record, *LEGACY_SESSION_CREATED_AT_FIELDS),
            child_value(record, "time", "created"),
        ),
        "updatedAt": first_not_none(
            root_or_info_value(record, *LEGACY_SESSION_UPDATED_AT_FIELDS),
            child_value(record, "time", "updated"),
        ),
    }


def _api_session_fields(record):
    return {
        "id": root_or_info_value(record, *API_SESSION_ID_FIELDS),
        "directory": first_not_none(
            root_or_info_value(record, *API_SESSION_DIRECTORY_FIELDS),
            child_value(record, "location", "directory"),
        ),
        "title": root_or_info_value(record, *API_SESSION_TITLE_FIELDS),
        "agent": root_or_info_value(record, *API_SESSION_AGENT_FIELDS),
        "model": root_or_info_value(record, *API_SESSION_MODEL_FIELDS),
        "tokens": root_or_info_value(record, *API_SESSION_TOKEN_FIELDS),
        "createdAt": first_not_none(
            root_or_info_value(record, *API_SESSION_CREATED_AT_FIELDS),
            child_value(record, "time", "created"),
        ),
        "updatedAt": first_not_none(
            root_or_info_value(record, *API_SESSION_UPDATED_AT_FIELDS),
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


API_SESSION_ADAPTER = SessionRouteAdapter(
    route="session_collection",
    version="api-v1",
    normalize_payload=_normalize_api_session_payload,
    normalize_record=_normalize_api_session_record,
)
LEGACY_SESSION_ADAPTER = SessionRouteAdapter(
    route="session_collection",
    version="legacy",
    normalize_payload=_normalize_legacy_session_payload,
    normalize_record=_normalize_legacy_session_record,
)
UNKNOWN_SESSION_ADAPTER = SessionRouteAdapter(
    route="session_collection",
    version="unknown",
    normalize_payload=_normalize_unknown_session_payload,
    normalize_record=unknown_session_record,
)
SESSION_ROUTE_ADAPTERS = {
    API_SESSION_ROUTE: API_SESSION_ADAPTER,
    LEGACY_SESSION_ROUTE: LEGACY_SESSION_ADAPTER,
}
DEFAULT_SESSION_ADAPTER = LEGACY_SESSION_ADAPTER
