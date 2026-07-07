from copy import deepcopy
from dataclasses import dataclass
from typing import Tuple

from opencode_session.schema_helpers import (
    collection_records,
    normalized_tokens,
    SESSION_ID_ALIASES,
    set_missing,
)
from opencode_session.schema_route_contract import RouteAdapterContract, child_field, route_field
from opencode_session.schema_session import NormalizedSessionRecord


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")
API_SESSION_ROUTE = "/api/session"
LEGACY_SESSION_ROUTE = "/session"
SESSION_KNOWN_FIELDS = ("id",)


API_SESSION_CONTRACT = RouteAdapterContract(
    route="session_collection",
    version="api-v1",
    fields=(
        route_field("id", "id"),
        route_field("directory", "directory", "cwd", children=(child_field("location", "directory"),)),
        route_field("title", "title"),
        route_field("agent", "agent"),
        route_field("model", "model"),
        route_field("tokens", "tokens", "tokenUsage", "usage"),
        route_field("createdAt", "createdAt", "created", children=(child_field("time", "created"),)),
        route_field("updatedAt", "updatedAt", "updated", children=(child_field("time", "updated"),)),
    ),
    known_fields=SESSION_KNOWN_FIELDS,
    minimum_field_sets=(("id",),),
)
LEGACY_SESSION_CONTRACT = RouteAdapterContract(
    route="session_collection",
    version="legacy",
    fields=(
        route_field("id", "id", *SESSION_ID_ALIASES),
        route_field("directory", "directory", "cwd", children=(child_field("location", "directory"),)),
        route_field("title", "title", "name"),
        route_field("agent", "agent", "agentID", "agentId", "agent_id"),
        route_field("model", "model", "modelID", "modelId", "model_id"),
        route_field("tokens", "tokens", "token", "tokenUsage", "token_usage", "usage"),
        route_field("createdAt", "createdAt", "created_at", "created", children=(child_field("time", "created"),)),
        route_field("updatedAt", "updatedAt", "updated_at", "updated", children=(child_field("time", "updated"),)),
    ),
    known_fields=SESSION_KNOWN_FIELDS,
    minimum_field_sets=(("id",),),
)
UNKNOWN_SESSION_CONTRACT = RouteAdapterContract(
    route="session_collection",
    version="unknown",
)
SESSION_VALUE_ALIASES = tuple((field.name, field.aliases) for field in LEGACY_SESSION_CONTRACT.fields)


@dataclass(frozen=True)
class SessionRouteAdapter:
    contract: RouteAdapterContract
    collection_names: Tuple[str, ...] = ()
    unknown: bool = False

    @property
    def route(self):
        return self.contract.route

    @property
    def version(self):
        return self.contract.version

    def read_fields(self, record):
        return self.contract.read_fields(record)

    def has_known_shape(self, fields):
        return self.contract.has_known_shape(fields)

    def has_minimum_shape(self, fields):
        return self.contract.has_minimum_shape(fields)

    def normalize_payload(self, payload):
        if self.unknown:
            return unknown_session_record(payload)
        return _normalize_session_payload(payload, self)

    def normalize_record(self, record):
        if self.unknown:
            return unknown_session_record(record)
        return _normalize_session_record(record, self)


def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_payload(payload)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_record(record)


def _normalize_session_payload(payload, adapter):
    if isinstance(payload, list):
        return [adapter.normalize_record(item) for item in payload]
    if not isinstance(payload, dict):
        return unknown_session_record(payload)

    normalized = dict(payload)
    for name in adapter.collection_names:
        records = normalized.get(name)
        if isinstance(records, list):
            normalized[name] = [adapter.normalize_record(item) for item in records]
            return normalized
        if name == "data" and isinstance(records, dict):
            normalized[name] = adapter.normalize_record(records)
            return normalized

    return adapter.normalize_record(normalized)


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


def _normalize_session_record(record, adapter) -> NormalizedSessionRecord:
    if not isinstance(record, dict):
        return unknown_session_record(record)
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = adapter.normalize_record(record["data"])
        return normalized

    fields = adapter.read_fields(record)
    if not adapter.has_known_shape(fields):
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
    contract=API_SESSION_CONTRACT,
    collection_names=("data", "sessions"),
)
LEGACY_SESSION_ADAPTER = SessionRouteAdapter(
    contract=LEGACY_SESSION_CONTRACT,
    collection_names=("sessions", "children", "data"),
)
UNKNOWN_SESSION_ADAPTER = SessionRouteAdapter(
    contract=UNKNOWN_SESSION_CONTRACT,
    unknown=True,
)
SESSION_ROUTE_ADAPTERS = {
    API_SESSION_ROUTE: API_SESSION_ADAPTER,
    LEGACY_SESSION_ROUTE: LEGACY_SESSION_ADAPTER,
}
DEFAULT_SESSION_ADAPTER = LEGACY_SESSION_ADAPTER
