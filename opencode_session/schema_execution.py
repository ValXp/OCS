from typing import TypedDict

from opencode_session.schema_helpers import JsonObject, JsonValue


class ExecutionResultRecord(TypedDict, total=False):
    session_id: str
    message_ids: JsonObject
    status: str
    raw_status: str
    terminal_state: str
    api_path: JsonObject
    execution_strategy: str
    fallback: JsonObject
    cost: JsonValue
    tokens: JsonValue
    text: str
