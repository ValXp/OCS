import uuid

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.api_profile import OpenCodeServerProfile, server_profile_from_capabilities
from opencode_session.formatting import compact_value
from opencode_session.schema_admission_adapter import admission_response_fields
from opencode_session.schema_common import first_present as _first_present


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

    profile = server_profile_from_capabilities(capabilities)
    message_id = message_id or f"msg_{uuid.uuid4().hex}"
    prompt_path = profile.route_plan["v2_prompt"]
    payload = profile.prompt_admission_payload(message_id, text, delivery)
    try:
        response = client.admit_prompt_response(session_id, payload, prompt_path)
    except OpenCodeApiError as error:
        if is_idempotent_admission_replay(error, message_id):
            return PromptAdmissionResult(
                record=admission_record(session_id, delivery, message_id, error.data, capabilities=capabilities, profile=profile),
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
        record=admission_record(session_id, delivery, message_id, response.data, capabilities=capabilities, profile=profile),
        body=response.body,
        message_id=message_id,
        payload=payload,
    )


def admission_record(session_id, delivery, message_id, data, *, capabilities, profile=None):
    profile = profile or server_profile_from_capabilities(capabilities)
    return profile.normalize_admission_record(session_id, delivery, message_id, data, capabilities=capabilities)


def prompt_admission_payload(message_id, text, delivery, prompt_path):
    return OpenCodeServerProfile.from_route_plan({"v2_prompt": prompt_path}).prompt_admission_payload(
        message_id,
        text,
        delivery,
    )


def format_admission_compact(admission):
    fields = [
        ("session", admission["session_id"]),
        ("message", admission["message_id"]),
        ("delivery", admission["delivery"]),
        ("status", admission["status"]),
        ("admitted", admission["admitted_sequence"]),
        ("promoted", admission["promoted_sequence"]),
    ]
    return "steer " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)


def is_idempotent_admission_replay(error, message_id):
    if error.status != 409 or not isinstance(error.data, dict):
        return False
    fields = admission_response_fields(error.data)
    if fields["message_id"] != message_id:
        return False
    data = error.data
    state = fields["state"]
    idempotency = fields["idempotency"]
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
