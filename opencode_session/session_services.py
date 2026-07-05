from dataclasses import dataclass
from pathlib import Path

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocker_inventory import blocker_counts_for_session, load_blocker_counts
from opencode_session.records import collection_sessions, first_present, session_record
from opencode_session.session_lifecycle import abort_record, is_session_not_found_error


@dataclass
class SessionCreateResult:
    session: dict
    raw_body: str


@dataclass
class SessionListResult:
    sessions: list
    blocker_counts: dict
    raw_body: str


@dataclass
class SessionInspectResult:
    session: dict
    blocker_counts: dict
    raw_body: str


@dataclass
class SessionDeleteResult:
    session_id: str
    response: object
    raw_body: str
    verified: str = "unreadable"


@dataclass
class SessionAbortResult:
    abort: dict
    raw_body: str


@dataclass
class SessionForkResult:
    fork: dict
    raw_body: str


@dataclass
class SessionChildrenResult:
    children: list
    raw_body: str


class SessionCommandError(Exception):
    pass


class SessionCommandService:
    def __init__(self, client):
        self.client = client

    def create(self, directory, *, agent=None, model=None):
        resolved_directory = str(Path(directory).resolve())
        try:
            response = self.client.create_session_response(resolved_directory, agent=agent, model=model)
        except OpenCodeApiError as error:
            raise SessionCommandError(str(error)) from error
        return SessionCreateResult(session=response.data, raw_body=response.body)

    def list(self, *, directory=None, agent=None, model=None, include_blockers=False):
        try:
            response = self.client.list_sessions_response()
        except OpenCodeApiError as error:
            raise SessionCommandError(str(error)) from error
        resolved_directory = str(Path(directory).resolve()) if directory else None
        sessions = _filter_sessions(
            collection_sessions(response.data),
            directory=resolved_directory,
            agent=agent,
            model=model,
        )
        blocker_counts = self._load_blocker_counts() if include_blockers else None
        return SessionListResult(sessions=sessions, blocker_counts=blocker_counts, raw_body=response.body)

    def inspect(self, session_id, *, include_blockers=False):
        try:
            response = self.client.get_session_response(session_id)
        except OpenCodeApiError as error:
            raise SessionCommandError(str(error)) from error
        blocker_counts = self._load_blocker_counts() if include_blockers else None
        return SessionInspectResult(session=response.data, blocker_counts=blocker_counts, raw_body=response.body)

    def delete(self, session_id):
        delete_response = None
        deleted = False
        try:
            delete_response = self.client.delete_session_response(session_id)
            deleted = True
            self.client.get_session(session_id)
        except OpenCodeApiError as error:
            if deleted and error.status == 404:
                return SessionDeleteResult(
                    session_id=session_id,
                    response=delete_response.data if delete_response else None,
                    raw_body=delete_response.body if delete_response else "",
                )
            raise SessionCommandError(str(error)) from error
        raise SessionCommandError(f"delete verification failed; session {session_id} is still readable")

    def abort(self, session_id):
        try:
            response = self.client.abort_session_response(session_id)
        except OpenCodeApiError as error:
            if is_session_not_found_error(error):
                raise SessionCommandError(f"session not found: {session_id}") from error
            raise SessionCommandError(str(error)) from error
        return SessionAbortResult(abort=abort_record(session_id, response.data), raw_body=response.body)

    def fork(self, session_id, *, message_id=None):
        try:
            response = self.client.fork_session_response(session_id, message_id=message_id)
        except OpenCodeApiError as error:
            if is_session_not_found_error(error):
                raise SessionCommandError(f"session not found: {session_id}") from error
            raise SessionCommandError(str(error)) from error
        return SessionForkResult(fork=fork_record(session_id, message_id, response.data), raw_body=response.body)

    def children(self, session_id, *, directory=None):
        try:
            response = self.client.list_child_sessions_response(session_id)
        except OpenCodeApiError as error:
            if is_session_not_found_error(error):
                raise SessionCommandError(f"session not found: {session_id}") from error
            raise SessionCommandError(str(error)) from error
        resolved_directory = str(Path(directory).resolve()) if directory else None
        children = _filter_sessions(collection_sessions(response.data), directory=resolved_directory)
        return SessionChildrenResult(children=children, raw_body=response.body)

    def _load_blocker_counts(self):
        try:
            return load_blocker_counts(self.client)
        except OpenCodeApiError as error:
            raise SessionCommandError(f"blocker summary failed: {error}") from error


def fork_record(parent_session_id, message_id, data):
    if not isinstance(data, dict):
        data = {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    return {
        "parent_session_id": first_present(
            data,
            "parentID",
            "parentId",
            "parentSessionID",
            "parentSessionId",
            "parent_session_id",
        )
        or parent_session_id,
        "session_id": first_present(data, "id", "sessionID", "sessionId", "childSessionID", "childSessionId")
        or first_present(session, "id", "sessionID", "sessionId"),
        "message_id": first_present(data, "messageID", "messageId", "message_id") or message_id,
        "response": data,
    }


def session_with_blocker_counts(session, counts):
    augmented = dict(session)
    augmented["blockers"] = counts_for_session(counts, session)
    return augmented


def counts_for_session(counts, session):
    if counts is None:
        return None
    return blocker_counts_for_session(counts, session_record(session).get("id"))


def _filter_sessions(sessions, *, directory=None, agent=None, model=None):
    filtered = []
    for session in sessions:
        normalized = session_record(session)
        if directory is not None and normalized.get("directory") != directory:
            continue
        if agent is not None and normalized.get("agent") != agent:
            continue
        if model is not None and normalized.get("model") != model:
            continue
        filtered.append(session)
    return filtered
