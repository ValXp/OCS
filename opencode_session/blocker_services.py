import json
from dataclasses import dataclass
from urllib.parse import quote

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocker_inventory import blocker_session_id, collection_blockers


@dataclass
class BlockerListResult:
    blockers: list
    raw_body: str


@dataclass
class BlockerResolutionResult:
    result: dict
    raw_body: str


class BlockerCommandError(Exception):
    def __init__(self, message, *, kind="unavailable"):
        super().__init__(message)
        self.kind = kind


class BlockerCommandService:
    def __init__(self, client):
        self.client = client

    def list_permissions(self, *, session_id=None):
        return self._list_blockers(self.client.list_permissions_response, "permissions", session_id=session_id)

    def list_questions(self, *, session_id=None):
        return self._list_blockers(self.client.list_questions_response, "questions", session_id=session_id)

    def reply_permission(self, request_id, reply, *, message=None):
        return self._resolve_blocker(
            lambda: self.client.reply_permission_response(request_id, reply, message=message),
            lambda error: _is_permission_request_not_found_error(error, request_id),
            f"permission request not found: {request_id}",
            lambda response: {
                "id": request_id,
                "reply": reply,
                "ok": bool(response.data),
                "response": response.data,
            },
        )

    def answer_question(self, request_id, answers, *, answers_json=None):
        parsed_answers = question_answers_from_values(answers, answers_json=answers_json)
        return self._resolve_blocker(
            lambda: self.client.answer_question_response(request_id, parsed_answers),
            lambda error: _is_question_request_not_found_error(error, request_id),
            f"question request not found: {request_id}",
            lambda response: {
                "id": request_id,
                "action": "answer",
                "ok": bool(response.data),
                "response": response.data,
                "answers": parsed_answers,
            },
        )

    def reject_question(self, request_id):
        return self._resolve_blocker(
            lambda: self.client.reject_question_response(request_id),
            lambda error: _is_question_request_not_found_error(error, request_id),
            f"question request not found: {request_id}",
            lambda response: {
                "id": request_id,
                "action": "reject",
                "ok": bool(response.data),
                "response": response.data,
            },
        )

    def _list_blockers(self, request_response, plural_name, *, session_id=None):
        try:
            response = request_response()
        except OpenCodeApiError as error:
            raise BlockerCommandError(str(error)) from error
        blockers = _filter_blockers_by_session(collection_blockers(response.data, plural_name), session_id)
        return BlockerListResult(blockers=blockers, raw_body=response.body)

    def _resolve_blocker(self, request_response, is_not_found_error, not_found_message, result_factory):
        try:
            response = request_response()
        except OpenCodeApiError as error:
            if is_not_found_error(error):
                raise BlockerCommandError(not_found_message, kind="noinput") from error
            raise BlockerCommandError(str(error)) from error
        return BlockerResolutionResult(result=result_factory(response), raw_body=response.body)


def question_answers_from_values(answers, *, answers_json=None):
    if answers_json is not None:
        if answers:
            raise BlockerCommandError("cannot combine positional answers with --answers-json", kind="dataerr")
        try:
            parsed_answers = json.loads(answers_json)
        except json.JSONDecodeError as error:
            raise BlockerCommandError(f"invalid --answers-json: {error}", kind="dataerr") from error
        if not _valid_question_answers(parsed_answers):
            raise BlockerCommandError("--answers-json must be a JSON array of string arrays", kind="dataerr")
        return parsed_answers
    if not answers:
        raise BlockerCommandError("at least one answer is required", kind="dataerr")
    return [[answer] for answer in answers]


def _filter_blockers_by_session(blockers, session_id):
    if session_id is None:
        return blockers
    return [blocker for blocker in blockers if blocker_session_id(blocker) == session_id]


def _valid_question_answers(answers):
    return isinstance(answers, list) and all(
        isinstance(answer, list) and all(isinstance(value, str) for value in answer) for answer in answers
    )


def _is_permission_request_not_found_error(error, request_id):
    return _is_blocker_resolution_not_found_error(error, "permission", request_id, ("reply",))


def _is_question_request_not_found_error(error, request_id):
    return _is_blocker_resolution_not_found_error(error, "question", request_id, ("reply", "reject"))


def _is_blocker_resolution_not_found_error(error, blocker_name, request_id, actions):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    quoted_id = quote(request_id, safe="")
    return method == "POST" and path in {f"/{blocker_name}/{quoted_id}/{action}" for action in actions}
