import uuid

from opencode_session.api_transport import OpenCodeApiError, OpenCodeApiTimeoutError
from opencode_session.session_lifecycle import abort_record
from opencode_session.timeout_boundary import TimeoutExpired


class BlockingExecutionTimeout(OpenCodeApiError):
    def __init__(self, message, *, prompt_id=None, abort_error=None):
        if abort_error:
            message = f"{message}; session abort failed: {abort_error}"
        super().__init__(message)
        self.prompt_id = prompt_id
        self.abort_error = abort_error


def new_session_message_id():
    return f"msg_{uuid.uuid4().hex}"


def execute_with_timeout_abort(client, session_id, prompt_id, callback):
    try:
        return callback()
    except (OpenCodeApiTimeoutError, TimeoutExpired, TimeoutError) as error:
        abort_error = _abort_timed_out_session(client, session_id)
        message = str(error) or f"blocking execution timed out for session {session_id}"
        raise BlockingExecutionTimeout(
            message,
            prompt_id=prompt_id,
            abort_error=abort_error,
        ) from error


def _abort_timed_out_session(client, session_id):
    try:
        response = client.abort_session_response(session_id)
        response_data = {"accepted": response.data} if isinstance(response.data, bool) else response.data
        abort = abort_record(session_id, response_data)
    except Exception as error:
        return str(error) or error.__class__.__name__
    if not abort["accepted"]:
        status = abort.get("raw_status") or abort.get("status") or "not accepted"
        return f"abort was not accepted ({status})"
    return None
