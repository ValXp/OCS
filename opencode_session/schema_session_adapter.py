from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import (
    FieldExtractor,
    FieldSource,
    NormalizedSessionRecord,
    collection_records,
    normalized_tokens,
    set_missing,
)


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")


@dataclass(frozen=True)
class SessionRouteSchema:
    extractor: FieldExtractor
    known_fields: tuple


@dataclass(frozen=True)
class SessionRouteAdapter:
    schema: SessionRouteSchema
    route: str = "session"
    version: str = "compatible"

    def normalize_payload(self, payload):
        if isinstance(payload, list):
            return [self.normalize_record(item) for item in payload]
        if not isinstance(payload, dict):
            return unknown_session_record(payload)

        normalized = dict(payload)
        data = normalized.get("data")
        if isinstance(data, list):
            normalized["data"] = [self.normalize_record(item) for item in data]
            return normalized
        if isinstance(data, dict):
            normalized["data"] = self.normalize_record(data)
            return normalized

        for name in ("sessions", "children"):
            records = normalized.get(name)
            if isinstance(records, list):
                normalized[name] = [self.normalize_record(item) for item in records]
                return normalized

        return self.normalize_record(normalized)

    def normalize_record(self, record) -> NormalizedSessionRecord:
        if not isinstance(record, dict):
            return unknown_session_record(record)
        if isinstance(record.get("data"), dict):
            normalized = dict(record)
            normalized["data"] = self.normalize_record(record["data"])
            return normalized
        if not self.is_known_record(record):
            return unknown_session_record(record)

        normalized = dict(record)
        normalized["schema_status"] = "known"
        set_missing(normalized, "id", self.field_value(record, "id"))
        set_missing(normalized, "directory", self.field_value(record, "directory"))
        set_missing(normalized, "title", self.field_value(record, "title"))
        set_missing(normalized, "agent", self.field_value(record, "agent"))
        set_missing(normalized, "model", self.field_value(record, "model"))
        set_missing(
            normalized,
            "tokens",
            normalized_tokens(self.field_value(record, "tokens")),
        )
        set_missing(normalized, "createdAt", self.field_value(record, "createdAt"))
        set_missing(normalized, "updatedAt", self.field_value(record, "updatedAt"))
        require_session_canonical_fields(normalized)
        return normalized

    def record(self, session):
        if isinstance(session, dict) and isinstance(session.get("data"), dict):
            return session["data"]
        return session if isinstance(session, dict) else {}

    def value(self, session, *names):
        session = self.record(session)
        return self.schema.extractor.named_value(session, *names)

    def field_value(self, session, field_name):
        session = self.record(session)
        return self.schema.extractor.value(session, field_name)

    def is_known_record(self, session):
        return self.schema.extractor.has_any(session, self.schema.known_fields)


def _session_schema(field_aliases):
    root_and_info_fields = {}
    for field_name, aliases in field_aliases.items():
        root_and_info_fields[field_name] = [
            FieldSource((), aliases),
            FieldSource(("info",), aliases),
        ]
    root_and_info_fields["directory"].append(FieldSource(("location",), ("directory",)))
    root_and_info_fields["createdAt"].append(FieldSource(("time",), ("created",)))
    root_and_info_fields["updatedAt"].append(FieldSource(("time",), ("updated",)))
    return SessionRouteSchema(
        extractor=FieldExtractor({field_name: tuple(sources) for field_name, sources in root_and_info_fields.items()}),
        known_fields=tuple(field_aliases),
    )


COMPATIBLE_SESSION_FIELD_ALIASES = {
    "id": ("id", "sessionID", "sessionId", "session_id"),
    "directory": ("directory", "cwd"),
    "title": ("title", "name"),
    "agent": ("agent", "agentID", "agentId", "agent_id"),
    "model": ("model", "modelID", "modelId", "model_id"),
    "tokens": ("tokens", "token", "tokenUsage", "token_usage", "usage"),
    "createdAt": ("createdAt", "created_at", "created"),
    "updatedAt": ("updatedAt", "updated_at", "updated"),
}

OPENAPI_SESSION_FIELD_ALIASES = {
    "id": ("id",),
    "directory": ("directory", "cwd"),
    "title": ("title",),
    "agent": ("agent",),
    "model": ("model",),
    "tokens": ("tokens", "tokenUsage", "usage"),
    "createdAt": ("createdAt", "created"),
    "updatedAt": ("updatedAt", "updated"),
}


COMPATIBLE_SESSION_SCHEMA = _session_schema(COMPATIBLE_SESSION_FIELD_ALIASES)
OPENAPI_SESSION_SCHEMA = _session_schema(OPENAPI_SESSION_FIELD_ALIASES)


@dataclass(frozen=True)
class OpenApiSessionRouteAdapter(SessionRouteAdapter):
    schema: SessionRouteSchema = OPENAPI_SESSION_SCHEMA
    version: str = "api-v1"


@dataclass(frozen=True)
class LegacySessionRouteAdapter(SessionRouteAdapter):
    schema: SessionRouteSchema = COMPATIBLE_SESSION_SCHEMA
    version: str = "legacy"


def unknown_session_record(raw) -> NormalizedSessionRecord:
    normalized = {field_name: None for field_name in SESSION_CANONICAL_FIELDS}
    normalized["schema_status"] = "unknown"
    normalized["raw"] = deepcopy(raw)
    return normalized


def require_session_canonical_fields(record):
    for field_name in SESSION_CANONICAL_FIELDS:
        record.setdefault(field_name, None)


def session_adapter_for_route(route_path=None, route_plan=None):
    path = route_path
    if path is None and isinstance(route_plan, dict):
        path = route_plan.get("session_collection")
    normalized_path = str(path or "").split("?", 1)[0].rstrip("/")
    if normalized_path == "/api/session":
        return OPENAPI_SESSION_ADAPTER
    if normalized_path == "/session":
        return LEGACY_SESSION_ADAPTER
    return SESSION_ADAPTER


def collection_sessions(collection):
    return collection_records(collection, "sessions", "children", "data")


SESSION_ADAPTER = SessionRouteAdapter(COMPATIBLE_SESSION_SCHEMA)
OPENAPI_SESSION_ADAPTER = OpenApiSessionRouteAdapter()
LEGACY_SESSION_ADAPTER = LegacySessionRouteAdapter()

def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_payload(payload)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_record(record)


session_record = SESSION_ADAPTER.record
session_value = SESSION_ADAPTER.value
