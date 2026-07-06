from dataclasses import dataclass
from typing import Optional

from opencode_session.api_transport import OpenCodeApiError


@dataclass(frozen=True)
class DisposableSessionCleanupOutcome:
    record: dict
    first_error: Optional[OpenCodeApiError] = None


class DisposableSessionLifecycle:
    def __init__(self, client):
        self.client = client

    def cleanup(self, session_ids):
        record = {"status": "done", "deleted": [], "verified": [], "errors": []}
        first_error = None
        for session_id in session_ids:
            error = self.delete_and_verify(session_id)
            if error is not None:
                if first_error is None:
                    first_error = error
                record["errors"].append({"session_id": session_id, "error": str(error)})
                record["status"] = "failed"
                continue
            record["deleted"].append(session_id)
            record["verified"].append(session_id)
        return DisposableSessionCleanupOutcome(record, first_error)

    def delete_and_verify(self, session_id):
        try:
            self.client.delete_session_response(session_id)
        except OpenCodeApiError as error:
            if error.status != 404:
                return error
        try:
            self.client.get_session(session_id)
        except OpenCodeApiError as error:
            if error.status == 404:
                return None
            return error
        return OpenCodeApiError(f"delete verification failed; session {session_id} is still readable")


def cleanup_disposable_sessions(client, session_ids):
    return DisposableSessionLifecycle(client).cleanup(session_ids)


def delete_and_verify_disposable_session(client, session_id):
    return DisposableSessionLifecycle(client).delete_and_verify(session_id)
