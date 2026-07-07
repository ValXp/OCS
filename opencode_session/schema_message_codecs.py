from dataclasses import dataclass

from opencode_session.schema_helpers import CAMEL_MESSAGE_ID_ALIASES, MESSAGE_ID_ALIASES
from opencode_session.schema_route_contract import (
    RouteAdapterContract,
    adapters_by_endpoint,
    adapters_by_route_path,
    route_field,
    route_path_key,
)


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")
MESSAGE_KNOWN_FIELDS = ("id", "role", "status", "text", "error")
MESSAGE_TEXT_FIELDS = ("text", "content")
MESSAGE_ERROR_FIELDS = ("error", "reason", "message")
MESSAGE_ID_MINIMUM_FIELD_SETS = (("error",), ("status",), ("id",))
FINAL_MESSAGE_MINIMUM_FIELD_SETS = (("error",), ("status",), ("id", "text"))
SESSION_MESSAGE_ROUTE = "session_message"
LEGACY_MESSAGE_ROUTE = "legacy_run_reply"
SESSION_MESSAGE_PATH = "/session/{sessionID}/message"
LEGACY_RUN_PATH = "/session/{sessionID}/run"
LEGACY_REPLY_PATH = "/session/{sessionID}/reply"


@dataclass(frozen=True)
class MessageRouteCodec:
    contract: RouteAdapterContract

    @property
    def route(self):
        return self.contract.route

    @property
    def version(self):
        return self.contract.version

    def read_fields(self, message):
        return self.contract.read_fields(message)

    def has_known_shape(self, fields):
        return self.contract.has_known_shape(fields)

    def has_minimum_shape(self, fields):
        return self.contract.has_minimum_shape(fields)


SESSION_MESSAGE_CONTRACT = RouteAdapterContract(
    route=SESSION_MESSAGE_ROUTE,
    version="session-message",
    fields=(
        route_field("id", "id", *CAMEL_MESSAGE_ID_ALIASES),
        route_field("role", "role"),
        route_field("status", "status", "state"),
        route_field("cost", "cost"),
        route_field("tokens", "tokens", "tokenUsage", "usage"),
        route_field("text", *MESSAGE_TEXT_FIELDS),
        route_field("error", *MESSAGE_ERROR_FIELDS),
    ),
    known_fields=MESSAGE_KNOWN_FIELDS,
    minimum_field_sets=FINAL_MESSAGE_MINIMUM_FIELD_SETS,
    route_paths=(SESSION_MESSAGE_PATH,),
    endpoint_names=("blocking_message",),
)
LEGACY_MESSAGE_CONTRACT = RouteAdapterContract(
    route=LEGACY_MESSAGE_ROUTE,
    version="legacy-run-reply",
    fields=(
        route_field("id", "id", *MESSAGE_ID_ALIASES),
        route_field("role", "role", "author", "speaker", "type", "kind"),
        route_field("status", "status", "state", "phase"),
        route_field("cost", "cost"),
        route_field("tokens", "tokens", "token", "tokenUsage", "token_usage", "usage"),
        route_field("text", *MESSAGE_TEXT_FIELDS),
        route_field("error", *MESSAGE_ERROR_FIELDS),
    ),
    known_fields=MESSAGE_KNOWN_FIELDS,
    minimum_field_sets=MESSAGE_ID_MINIMUM_FIELD_SETS,
    route_paths=(LEGACY_RUN_PATH, LEGACY_REPLY_PATH),
    endpoint_names=("legacy_run", "legacy_reply"),
)
UNKNOWN_MESSAGE_CONTRACT = RouteAdapterContract(
    route="unknown",
    version="unknown",
)

SESSION_MESSAGE_CODEC = MessageRouteCodec(SESSION_MESSAGE_CONTRACT)
LEGACY_MESSAGE_CODEC = MessageRouteCodec(LEGACY_MESSAGE_CONTRACT)
UNKNOWN_MESSAGE_CODEC = MessageRouteCodec(UNKNOWN_MESSAGE_CONTRACT)
MESSAGE_ROUTE_CODECS = {
    SESSION_MESSAGE_ROUTE: SESSION_MESSAGE_CODEC,
    LEGACY_MESSAGE_ROUTE: LEGACY_MESSAGE_CODEC,
}
MESSAGE_ROUTE_PATH_CODECS = adapters_by_route_path((SESSION_MESSAGE_CODEC, LEGACY_MESSAGE_CODEC))
MESSAGE_ENDPOINT_CODECS = adapters_by_endpoint((SESSION_MESSAGE_CODEC, LEGACY_MESSAGE_CODEC))
DEFAULT_MESSAGE_CODEC = LEGACY_MESSAGE_CODEC
MESSAGE_VALUE_ALIASES = tuple((field.name, field.aliases) for field in LEGACY_MESSAGE_CONTRACT.fields)


def message_codec_for_route(route=None):
    if route is None:
        return DEFAULT_MESSAGE_CODEC
    return (
        MESSAGE_ROUTE_CODECS.get(route)
        or MESSAGE_ROUTE_PATH_CODECS.get(route_path_key(route))
        or UNKNOWN_MESSAGE_CODEC
    )


def message_codec_for_endpoint(endpoint, route_path=None):
    path_codec = MESSAGE_ROUTE_PATH_CODECS.get(route_path_key(route_path))
    if path_codec is not None:
        return path_codec
    return MESSAGE_ENDPOINT_CODECS.get(endpoint, UNKNOWN_MESSAGE_CODEC)
