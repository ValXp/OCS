from typing import Optional, TypedDict

from opencode_session.schema_helpers import JsonValue


class NormalizedEventRecord(TypedDict, total=False):
    kind: str
    session_id: Optional[str]
    target_session_id: Optional[str]
    type: Optional[str]
    message_id: Optional[str]
    status: Optional[str]
    raw_status: Optional[str]
    delivery: Optional[str]
    text: Optional[str]
    tool: Optional[str]
    call_id: Optional[str]
    step: Optional[str]
    title: Optional[str]
    blocker: Optional[str]
    blocker_id: Optional[str]
    question: Optional[str]
    error: Optional[str]
    reason: Optional[str]
    schema_status: str
    raw: JsonValue
