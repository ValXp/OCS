from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import (
    NormalizedSessionRecord,
    collection_records,
    first_present,
    normalized_tokens,
    set_missing,
)


SESSION_CANONICAL_FIELDS = ("id", "directory", "title", "agent", "model", "tokens", "createdAt", "updatedAt")


@dataclass(frozen=True)
class SessionRouteAdapter:
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
        set_missing(normalized, "id", self.value(record, *self.id_fields))
        set_missing(normalized, "directory", self.value(record, *self.directory_fields))
        set_missing(normalized, "title", self.value(record, *self.title_fields))
        set_missing(normalized, "agent", self.value(record, *self.agent_fields))
        set_missing(normalized, "model", self.value(record, *self.model_fields))
        set_missing(
            normalized,
            "tokens",
            normalized_tokens(self.value(record, *self.token_fields)),
        )
        set_missing(normalized, "createdAt", self.value(record, *self.created_fields))
        set_missing(normalized, "updatedAt", self.value(record, *self.updated_fields))
        require_session_canonical_fields(normalized)
        return normalized

    def record(self, session):
        if isinstance(session, dict) and isinstance(session.get("data"), dict):
            return session["data"]
        return session if isinstance(session, dict) else {}

    def value(self, session, *names):
        session = self.record(session)
        value = first_present(session, *names)
        if value is not None:
            return value
        info = session.get("info")
        value = first_present(info, *names)
        if value is not None:
            return value
        location = session.get("location")
        if isinstance(location, dict):
            for name in names:
                if name in {"directory", "cwd"} and location.get("directory") is not None:
                    return location.get("directory")
        time = session.get("time")
        if isinstance(time, dict):
            for name in names:
                if name in {"createdAt", "created_at"} and time.get("created") is not None:
                    return time.get("created")
                if name in {"updatedAt", "updated_at"} and time.get("updated") is not None:
                    return time.get("updated")
        return None

    def is_known_record(self, session):
        return any(
            self.value(session, *names) is not None
            for names in self.known_field_groups
        )

    @property
    def id_fields(self):
        return ("id", "sessionID", "sessionId", "session_id")

    @property
    def directory_fields(self):
        return ("directory", "cwd")

    @property
    def title_fields(self):
        return ("title", "name")

    @property
    def agent_fields(self):
        return ("agent", "agentID", "agentId", "agent_id")

    @property
    def model_fields(self):
        return ("model", "modelID", "modelId", "model_id")

    @property
    def token_fields(self):
        return ("tokens", "token", "tokenUsage", "token_usage", "usage")

    @property
    def created_fields(self):
        return ("createdAt", "created_at", "created")

    @property
    def updated_fields(self):
        return ("updatedAt", "updated_at", "updated")

    @property
    def known_field_groups(self):
        return (
            self.id_fields,
            self.directory_fields,
            self.title_fields,
            self.agent_fields,
            self.model_fields,
            self.token_fields,
            self.created_fields,
            self.updated_fields,
        )


@dataclass(frozen=True)
class OpenApiSessionRouteAdapter(SessionRouteAdapter):
    version: str = "api-v1"

    @property
    def id_fields(self):
        return ("id",)

    @property
    def directory_fields(self):
        return ("directory", "cwd")

    @property
    def title_fields(self):
        return ("title",)

    @property
    def agent_fields(self):
        return ("agent",)

    @property
    def model_fields(self):
        return ("model",)

    @property
    def token_fields(self):
        return ("tokens", "tokenUsage", "usage")

    @property
    def created_fields(self):
        return ("createdAt", "created")

    @property
    def updated_fields(self):
        return ("updatedAt", "updated")


@dataclass(frozen=True)
class LegacySessionRouteAdapter(SessionRouteAdapter):
    version: str = "legacy"

    def value(self, session, *names):
        session = self.record(session)
        value = first_present(session, *names)
        if value is not None:
            return value
        info = session.get("info")
        value = first_present(info, *names)
        if value is not None:
            return value
        location = session.get("location")
        if isinstance(location, dict):
            for name in names:
                if name in {"directory", "cwd"} and location.get("directory") is not None:
                    return location.get("directory")
        time = session.get("time")
        if isinstance(time, dict):
            for name in names:
                if name in {"createdAt", "created_at"} and time.get("created") is not None:
                    return time.get("created")
                if name in {"updatedAt", "updated_at"} and time.get("updated") is not None:
                    return time.get("updated")
        return None


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


SESSION_ADAPTER = SessionRouteAdapter()
OPENAPI_SESSION_ADAPTER = OpenApiSessionRouteAdapter()
LEGACY_SESSION_ADAPTER = LegacySessionRouteAdapter()

def normalize_session_payload(payload, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_payload(payload)


def normalize_session_record(record, *, route_path=None, route_plan=None):
    return session_adapter_for_route(route_path, route_plan).normalize_record(record)


session_record = SESSION_ADAPTER.record
session_value = SESSION_ADAPTER.value
