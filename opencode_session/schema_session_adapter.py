from dataclasses import dataclass

from opencode_session.schema_common import NormalizedSessionRecord, first_present, normalized_tokens, set_missing


@dataclass(frozen=True)
class SessionRouteAdapter:
    route: str = "session"
    version: str = "opencode-compatible"

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
        set_missing(normalized, "id", self.value(record, "id", "sessionID", "sessionId", "session_id"))
        set_missing(normalized, "directory", self.value(record, "directory", "cwd"))
        set_missing(normalized, "title", self.value(record, "title", "name"))
        set_missing(normalized, "agent", self.value(record, "agent", "agentID", "agentId", "agent_id"))
        set_missing(normalized, "model", self.value(record, "model", "modelID", "modelId", "model_id"))
        set_missing(
            normalized,
            "tokens",
            normalized_tokens(self.value(record, "tokens", "token", "tokenUsage", "token_usage", "usage")),
        )
        set_missing(normalized, "createdAt", self.value(record, "createdAt", "created_at", "created"))
        set_missing(normalized, "updatedAt", self.value(record, "updatedAt", "updated_at", "updated"))
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
            for names in (
                ("id", "sessionID", "sessionId", "session_id"),
                ("directory", "cwd"),
                ("title", "name"),
                ("agent", "agentID", "agentId", "agent_id"),
                ("model", "modelID", "modelId", "model_id"),
                ("tokens", "token", "tokenUsage", "token_usage", "usage"),
                ("createdAt", "created_at", "created"),
                ("updatedAt", "updated_at", "updated"),
            )
        )


def unknown_session_record(raw) -> NormalizedSessionRecord:
    if isinstance(raw, dict):
        normalized = dict(raw)
        normalized["schema_status"] = "unknown"
        return normalized
    return {"schema_status": "unknown", "raw": raw}


SESSION_ADAPTER = SessionRouteAdapter()

normalize_session_payload = SESSION_ADAPTER.normalize_payload
normalize_session_record = SESSION_ADAPTER.normalize_record
session_record = SESSION_ADAPTER.record
session_value = SESSION_ADAPTER.value
