from dataclasses import dataclass

from opencode_session.schema_admission import NormalizedAdmissionRecord
from opencode_session.schema_helpers import (
    CAMEL_MESSAGE_ID_ALIASES,
    DELIVERY_ALIASES,
    PROMPT_ID_ALIASES,
    SESSION_ID_ALIASES,
    STATUS_ALIASES,
    first_present,
    root_or_info_value,
)
from opencode_session.status import short_status


ADMISSION_MESSAGE_ID_ALIASES = (*CAMEL_MESSAGE_ID_ALIASES, *PROMPT_ID_ALIASES, "id")
ADMITTED_SEQUENCE_ALIASES = ("admittedSeq", "admittedSequence", "admitted_sequence", "sequence")
PROMOTED_SEQUENCE_ALIASES = ("promotedSeq", "promotedSequence", "promoted_sequence")


@dataclass(frozen=True)
class AdmissionRouteAdapter:
    route: str = "session_prompt"
    version: str = "opencode-compatible"

    def normalize_record(self, session_id, delivery, message_id, data, *, capabilities) -> NormalizedAdmissionRecord:
        fields = admission_response_fields(data)
        state = fields["state"] or "admitted"
        return {
            "session_id": fields["session_id"] or session_id,
            "message_id": fields["message_id"] or message_id,
            "delivery": fields["delivery"] or delivery,
            "state": state,
            "raw_state": state,
            "status": short_status(state),
            "terminal_state": None,
            "api_path": capabilities["route_availability"]["v2_prompt"]["path"],
            "fallback": {
                "available": capabilities["legacy_fallback_available"],
                "strategy": "legacy_run_reply",
                "used": False,
            },
            "admitted_sequence": fields["admitted_sequence"],
            "promoted_sequence": fields["promoted_sequence"],
        }


def admission_response_fields(data):
    data = _admission_data(data)
    return {
        "session_id": root_or_info_value(data, *SESSION_ID_ALIASES),
        "message_id": root_or_info_value(data, *ADMISSION_MESSAGE_ID_ALIASES),
        "delivery": first_present(data, *DELIVERY_ALIASES),
        "state": first_present(data, *STATUS_ALIASES),
        "idempotency": first_present(data, "idempotency", "idempotencyStatus"),
        "admitted_sequence": first_present(data, *ADMITTED_SEQUENCE_ALIASES),
        "promoted_sequence": first_present(data, *PROMOTED_SEQUENCE_ALIASES),
    }


def _admission_data(data):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    if isinstance(data, dict):
        return data
    return {}


ADMISSION_ADAPTER = AdmissionRouteAdapter()

normalize_admission_record = ADMISSION_ADAPTER.normalize_record
