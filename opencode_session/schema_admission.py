from typing import TypedDict

from opencode_session.schema_helpers import JsonObject, JsonValue


class NormalizedAdmissionRecord(TypedDict):
    session_id: str
    message_id: str
    delivery: str
    state: str
    raw_state: str
    status: str
    terminal_state: JsonValue
    api_path: str
    fallback: JsonObject
    admitted_sequence: JsonValue
    promoted_sequence: JsonValue
