from typing import Optional, TypedDict

from opencode_session.schema_helpers import JsonValue


class NormalizedMessageRecord(TypedDict):
    id: Optional[str]
    role: Optional[str]
    status: Optional[str]
    raw_status: Optional[str]
    cost: JsonValue
    tokens: JsonValue
    text: str
    raw: JsonValue
