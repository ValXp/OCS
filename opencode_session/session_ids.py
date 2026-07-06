from opencode_session.api_transport import OpenCodeApiError
from opencode_session.schema_session_adapter import session_value


def require_session_id(create_response, context="session creation"):
    session_id = session_value(create_response.data, "id")
    if not session_id:
        raise OpenCodeApiError(f"{context} returned malformed response: missing session id")
    return session_id
