from typing import TypedDict

from opencode_session.schema_helpers import JsonObject


class CapabilitiesRecord(TypedDict, total=False):
    route_availability: JsonObject
    blocking_message_available: bool
    blocking_execution_available: bool
    legacy_fallback_available: bool
    wait_route: JsonObject
    prompt_route: JsonObject
