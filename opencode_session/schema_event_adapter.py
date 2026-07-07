from copy import deepcopy

from opencode_session.schema_event import NormalizedEventRecord
from opencode_session.schema_event_codecs import (
    ABORT_STATUSES,
    ADMISSION_EVENT_TYPES,
    API_EVENT_CODEC,
    API_EVENT_CONTRACT,
    API_EVENT_ROUTE,
    BLOCKER_EVENT_TYPES,
    DecodedEvent,
    ERROR_EVENT_TYPES,
    EVENT_BLOCKER_ID_FIELDS,
    EVENT_CALL_ID_FIELDS,
    EVENT_KIND_CONTRACTS,
    EVENT_KIND_CONTRACTS_BY_KIND,
    EVENT_MESSAGE_ID_FIELDS,
    EVENT_PERMISSION_ID_FIELDS,
    EVENT_QUESTION_ID_FIELDS,
    EVENT_ROUTE_CODECS,
    EVENT_ROUTE_FIELDS,
    EVENT_STATUS_FIELDS,
    EVENT_STEP_FIELDS,
    EVENT_TOOL_NAME_FIELDS,
    EventKindContract,
    KNOWN_EVENT_CODEC,
    KNOWN_EVENT_CONTRACT,
    LEGACY_EVENT_CODEC,
    LEGACY_EVENT_CONTRACT,
    LEGACY_EVENT_ROUTES,
    PROMPT_EVENT_TYPES,
    STATUS_EVENT_TYPES,
    STEP_EVENT_TYPES,
    SUCCESS_STATUSES,
    TEXT_EVENT_TYPES,
    TOOL_EVENT_TYPES,
    UNKNOWN_EVENT_CODEC,
    UNKNOWN_EVENT_CONTRACT,
    KnownEventRouteCodec,
    decoded_event_kind,
    event_codec_for_route,
)
from opencode_session.schema_helpers import set_if_present
from opencode_session.status import short_status


class EventRouteAdapter:
    def __init__(self, codec):
        self.codec = codec

    @property
    def contract(self):
        return self.codec.contract

    @property
    def route(self):
        return self.codec.route

    @property
    def version(self):
        return self.codec.version

    def decode(self, event):
        return self.codec.decode(event)

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return _normalize_decoded_event(event, self.decode(event), target_session_id)


class ApiEventRouteDecoder(EventRouteAdapter):
    def __init__(self):
        super().__init__(API_EVENT_CODEC)


class LegacyEventRouteDecoder(EventRouteAdapter):
    def __init__(self):
        super().__init__(LEGACY_EVENT_CODEC)


class KnownEventRouteDecoder(EventRouteAdapter):
    def __init__(self, decoders=None):
        codec = KNOWN_EVENT_CODEC
        if decoders is not None:
            codec = KnownEventRouteCodec(tuple(_codec_from_adapter(decoder) for decoder in decoders))
        super().__init__(codec)


class UnknownEventRouteDecoder(EventRouteAdapter):
    def __init__(self):
        super().__init__(UNKNOWN_EVENT_CODEC)

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return unknown_event_record(event)


def _codec_from_adapter(adapter):
    return getattr(adapter, "codec", adapter)


def _normalize_decoded_event(raw_event, decoded, target_session_id):
    if decoded is None:
        return unknown_event_record(raw_event)
    if target_session_id is not None and decoded.session_id is not None and decoded.session_id != target_session_id:
        return ignored_event_record(decoded.session_id, target_session_id, decoded.event_type)

    kind = decoded_event_kind(decoded)
    if kind == "unknown":
        return unknown_event_record(raw_event, event_type=decoded.event_type, session_id=decoded.session_id)

    normalized = {"kind": kind, "schema_status": "known"}
    set_if_present(normalized, "session_id", decoded.session_id)
    set_if_present(normalized, "type", decoded.event_type)
    set_if_present(normalized, "message_id", decoded.message_id)
    set_if_present(normalized, "delivery", decoded.delivery)
    set_if_present(normalized, "text", decoded.text)
    set_if_present(normalized, "tool", decoded.tool)
    set_if_present(normalized, "call_id", decoded.call_id)
    set_if_present(normalized, "step", decoded.step)
    set_if_present(normalized, "title", decoded.title)
    set_if_present(normalized, "blocker", decoded.blocker)
    set_if_present(normalized, "blocker_id", decoded.blocker_id)
    set_if_present(normalized, "question", decoded.question)
    set_if_present(normalized, "error", decoded.error)
    if decoded.status is not None:
        normalized["status"] = short_status(decoded.status)
        if normalized["status"] != decoded.status:
            normalized["raw_status"] = decoded.status
    return normalized


def _event_kind(decoded):
    return decoded_event_kind(decoded)


def ignored_event_record(session_id, target_session_id, event_type) -> NormalizedEventRecord:
    normalized = {
        "kind": "ignored",
        "schema_status": "known",
        "target_session_id": target_session_id,
        "reason": "session_mismatch",
    }
    set_if_present(normalized, "session_id", session_id)
    set_if_present(normalized, "type", event_type)
    return normalized


def unknown_event_record(raw, *, event_type=None, session_id=None) -> NormalizedEventRecord:
    normalized = {
        "kind": "unknown",
        "schema_status": "unknown",
        "reason": "unrecognized_event_shape",
        "raw": deepcopy(raw),
    }
    set_if_present(normalized, "session_id", session_id)
    set_if_present(normalized, "type", event_type)
    return normalized


def event_adapter_for_route(route_path=None):
    return _adapter_for_codec(event_codec_for_route(route_path))


def _adapter_for_codec(codec):
    if codec is API_EVENT_CODEC:
        return API_EVENT_DECODER
    if codec is LEGACY_EVENT_CODEC:
        return LEGACY_EVENT_DECODER
    if codec is KNOWN_EVENT_CODEC:
        return KNOWN_EVENT_DECODER
    return UNKNOWN_EVENT_DECODER


API_EVENT_DECODER = ApiEventRouteDecoder()
LEGACY_EVENT_DECODER = LegacyEventRouteDecoder()
KNOWN_EVENT_DECODER = KnownEventRouteDecoder()
UNKNOWN_EVENT_DECODER = UnknownEventRouteDecoder()
EVENT_ROUTE_DECODERS = {
    path: _adapter_for_codec(codec)
    for path, codec in EVENT_ROUTE_CODECS.items()
}
EVENT_ADAPTER = KNOWN_EVENT_DECODER
OPENAPI_EVENT_ADAPTER = API_EVENT_DECODER
LEGACY_EVENT_ADAPTER = LEGACY_EVENT_DECODER


def normalize_event_record(event, target_session_id=None, *, route_path=None):
    return event_adapter_for_route(route_path).normalize_record(event, target_session_id)
