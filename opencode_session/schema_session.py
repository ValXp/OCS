from typing import Optional, TypedDict

from opencode_session.schema_helpers import JsonObject, JsonValue


class NormalizedSessionRecord(TypedDict):
    id: Optional[str]
    directory: Optional[str]
    title: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    tokens: JsonValue
    createdAt: JsonValue
    updatedAt: JsonValue
    schema_status: str
    raw: JsonValue


class NormalizedAbortRecord(TypedDict):
    session_id: str
    accepted: bool
    status: Optional[str]
    raw_status: JsonValue
    response: JsonObject
