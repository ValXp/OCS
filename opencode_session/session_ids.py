from opencode_session.api_client import OpenCodeApiError
from opencode_session.records import session_value


SESSION_ID_FIELDS = ("id", "sessionID", "sessionId")


def require_session_id(create_response, context="session creation"):
    session_id = session_value(create_response.data, *SESSION_ID_FIELDS)
    if not session_id:
        raise OpenCodeApiError(f"{context} returned malformed response: missing session id")
    return session_id
