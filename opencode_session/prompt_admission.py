import uuid

from opencode_session.api_client import OpenCodeApiError
from opencode_session.records import first_present as _first_present
from opencode_session.status import short_status


UNSUPPORTED_BEHAVIOR_STATUSES = {400, 404, 405, 415, 422}


class PromptAdmissionResult:
    def __init__(self, *, record, body, message_id, payload, replayed=False):
        self.record = record
        self.body = body
        self.message_id = message_id
        self.payload = payload
        self.replayed = replayed


class PromptAdmissionUnsupported(Exception):
    pass


class PromptAdmissionFailure(Exception):
    pass


def admit_prompt(client, capabilities, session_id, text, delivery, *, message_id=None, map_unsupported=True):
    if not capabilities["v2_prompt_support"]:
        raise PromptAdmissionUnsupported(_unsupported_prompt_capability_message())

    message_id = message_id or f"msg_{uuid.uuid4().hex}"
    prompt_path = capabilities["route_availability"]["v2_prompt"]["path"]
    payload = prompt_admission_payload(message_id, text, delivery, prompt_path)
    try:
        response = client.admit_prompt_response(session_id, payload, prompt_path)
    except OpenCodeApiError as error:
        if is_idempotent_admission_replay(error, message_id):
            return PromptAdmissionResult(
                record=admission_record(session_id, delivery, message_id, error.data, capabilities=capabilities),
                body=error.body or "",
                message_id=message_id,
                payload=payload,
                replayed=True,
            )
        if not map_unsupported:
            raise
        if error.status in UNSUPPORTED_BEHAVIOR_STATUSES:
            raise PromptAdmissionUnsupported(
                f"unsupported v2 prompt behavior; {_api_error_detail(error)}; legacy run/reply fallback is not used"
            ) from error
        raise PromptAdmissionFailure(
            f"prompt admission failed; {error}; legacy run/reply fallback is not used"
        ) from error

    return PromptAdmissionResult(
        record=admission_record(session_id, delivery, message_id, response.data, capabilities=capabilities),
        body=response.body,
        message_id=message_id,
        payload=payload,
    )


def admission_record(session_id, delivery, message_id, data, *, capabilities):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        data = {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    state = _first_present(data, "state", "status", "phase") or "admitted"
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id")
        or _first_present(info, "sessionID", "sessionId", "session_id")
        or session_id,
        "message_id": _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
        or message_id,
        "delivery": _first_present(data, "delivery", "deliveryMode", "mode") or delivery,
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
        "admitted_sequence": _first_present(data, "admittedSeq", "admittedSequence", "admitted_sequence", "sequence"),
        "promoted_sequence": _first_present(data, "promotedSeq", "promotedSequence", "promoted_sequence"),
    }


def prompt_admission_payload(message_id, text, delivery, prompt_path):
    if prompt_path.split("?", 1)[0].rstrip("/") == "/api/session/{sessionID}/prompt":
        return {"id": message_id, "prompt": {"text": text}, "delivery": delivery}
    return {
        "messageID": message_id,
        "parts": [{"type": "text", "text": text}],
        "delivery": delivery,
    }


def is_idempotent_admission_replay(error, message_id):
    if error.status != 409 or not isinstance(error.data, dict):
        return False
    data = error.data
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    response_message_id = (
        _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
    )
    if response_message_id != message_id:
        return False
    state = _first_present(data, "state", "status", "phase")
    idempotency = _first_present(data, "idempotency", "idempotencyStatus")
    return (
        data.get("duplicate") is True
        or data.get("idempotent") is True
        or idempotency in {"duplicate", "replayed", "existing"}
        or state in {"admitted", "promoted", "running", "completed", "failed"}
    )


def _unsupported_prompt_capability_message():
    return (
        "unsupported v2 prompt capability; durable prompt admission requires "
        "POST /api/session/{sessionID}/prompt or POST /session/{sessionID}/prompt_async; "
        "legacy run/reply fallback is not used for steer admission"
    )


def _api_error_detail(error):
    if isinstance(error.data, dict):
        for name in ("error", "message", "detail"):
            value = error.data.get(name)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = _first_present(value, "message", "detail", "error")
                if isinstance(nested, str):
                    return nested
    return str(error)
