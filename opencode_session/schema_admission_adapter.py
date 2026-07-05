from dataclasses import dataclass

from opencode_session.schema_common import NormalizedAdmissionRecord, first_present
from opencode_session.status import short_status


@dataclass(frozen=True)
class AdmissionRouteAdapter:
    route: str = "session_prompt"
    version: str = "opencode-compatible"

    def normalize_record(self, session_id, delivery, message_id, data, *, capabilities) -> NormalizedAdmissionRecord:
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            data = {}
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        state = first_present(data, "state", "status", "phase") or "admitted"
        return {
            "session_id": first_present(data, "sessionID", "sessionId", "session_id")
            or first_present(info, "sessionID", "sessionId", "session_id")
            or session_id,
            "message_id": first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
            or first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
            or message_id,
            "delivery": first_present(data, "delivery", "deliveryMode", "mode") or delivery,
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
            "admitted_sequence": first_present(data, "admittedSeq", "admittedSequence", "admitted_sequence", "sequence"),
            "promoted_sequence": first_present(data, "promotedSeq", "promotedSequence", "promoted_sequence"),
        }


ADMISSION_ADAPTER = AdmissionRouteAdapter()

normalize_admission_record = ADMISSION_ADAPTER.normalize_record
