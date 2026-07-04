import json
from urllib.parse import quote

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocker_inventory import collection_blockers as _collection_blockers
from opencode_session.blocker_inventory import blocker_counts_for_session, blocker_session_id as _blocker_session_id
from opencode_session.blocker_inventory import load_blocker_counts
from opencode_session.formatting import (
    compact_bool as _compact_bool,
    compact_list as _compact_list,
    compact_value as _compact_value,
    format_table as _format_table,
    write_raw as _write_raw,
)
from opencode_session.records import first_present as _first_present


def add_blocker_parsers(subparsers, *, add_server_argument, add_output_arguments, handler):
    permission_parser = subparsers.add_parser("permission")
    permission_subparsers = permission_parser.add_subparsers(dest="permission_command")
    permission_subparsers.required = True
    permission_list_parser = permission_subparsers.add_parser("list")
    permission_list_parser.add_argument("--session", dest="session_id", help="only show requests for this session")
    add_server_argument(permission_list_parser)
    add_output_arguments(permission_list_parser)
    permission_list_parser.set_defaults(command_handler=handler)
    permission_reply_parser = permission_subparsers.add_parser("reply")
    permission_reply_parser.add_argument("request_id", help="permission request ID to resolve")
    permission_reply_parser.add_argument("reply", choices=("once", "always", "reject"), help="permission response")
    permission_reply_parser.add_argument("--message", help="feedback to send with a rejected permission")
    add_server_argument(permission_reply_parser)
    add_output_arguments(permission_reply_parser)
    permission_reply_parser.set_defaults(command_handler=handler)

    question_parser = subparsers.add_parser("question")
    question_subparsers = question_parser.add_subparsers(dest="question_command")
    question_subparsers.required = True
    question_list_parser = question_subparsers.add_parser("list")
    question_list_parser.add_argument("--session", dest="session_id", help="only show requests for this session")
    add_server_argument(question_list_parser)
    add_output_arguments(question_list_parser)
    question_list_parser.set_defaults(command_handler=handler)
    question_answer_parser = question_subparsers.add_parser("answer")
    question_answer_parser.add_argument("request_id", help="question request ID to answer")
    question_answer_parser.add_argument("answers", nargs="*", help="answer label/text; repeat for multiple questions")
    question_answer_parser.add_argument("--answers-json", help="JSON array of answer arrays for multi-select questions")
    add_server_argument(question_answer_parser)
    add_output_arguments(question_answer_parser)
    question_answer_parser.set_defaults(command_handler=handler)
    question_reject_parser = question_subparsers.add_parser("reject")
    question_reject_parser.add_argument("request_id", help="question request ID to reject")
    add_server_argument(question_reject_parser)
    add_output_arguments(question_reject_parser)
    question_reject_parser.set_defaults(command_handler=handler)

    return {"permission": permission_parser, "question": question_parser}


def handle_blocker_command(
    args,
    *,
    print_error,
    unavailable_exit,
    noinput_exit,
    dataerr_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.command == "permission":
        return handle_permission_command(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            noinput_exit=noinput_exit,
        )
    if args.command == "question":
        return handle_question_command(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            noinput_exit=noinput_exit,
            dataerr_exit=dataerr_exit,
        )
    return 64


def handle_permission_command(args, client, *, print_error, unavailable_exit, noinput_exit):
    if args.permission_command == "list":
        return _handle_blocker_list(
            args,
            client.list_permissions_response,
            "permissions",
            _format_permission_compact,
            _format_permission_table,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
        )
    if args.permission_command == "reply":
        return _handle_blocker_resolution(
            args,
            lambda: client.reply_permission_response(args.request_id, args.reply, message=args.message),
            lambda error: _is_permission_request_not_found_error(error, args.request_id),
            f"permission request not found: {args.request_id}",
            lambda response: {
                "id": args.request_id,
                "reply": args.reply,
                "ok": bool(response.data),
                "response": response.data,
            },
            _format_permission_reply_compact,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            noinput_exit=noinput_exit,
        )
    return 64


def handle_question_command(args, client, *, print_error, unavailable_exit, noinput_exit, dataerr_exit):
    if args.question_command == "list":
        return _handle_blocker_list(
            args,
            client.list_questions_response,
            "questions",
            _format_question_compact,
            _format_question_table,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
        )
    if args.question_command == "answer":
        try:
            answers = _question_answers_from_args(args)
        except ValueError as error:
            print_error(str(error))
            return dataerr_exit
        return _handle_blocker_resolution(
            args,
            lambda: client.answer_question_response(args.request_id, answers),
            lambda error: _is_question_request_not_found_error(error, args.request_id),
            f"question request not found: {args.request_id}",
            lambda response: {
                "id": args.request_id,
                "action": "answer",
                "ok": bool(response.data),
                "response": response.data,
                "answers": answers,
            },
            _format_question_resolution_compact,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            noinput_exit=noinput_exit,
        )
    if args.question_command == "reject":
        return _handle_blocker_resolution(
            args,
            lambda: client.reject_question_response(args.request_id),
            lambda error: _is_question_request_not_found_error(error, args.request_id),
            f"question request not found: {args.request_id}",
            lambda response: {
                "id": args.request_id,
                "action": "reject",
                "ok": bool(response.data),
                "response": response.data,
            },
            _format_question_resolution_compact,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            noinput_exit=noinput_exit,
        )
    return 64


def _handle_blocker_list(args, request_response, plural_name, compact_formatter, table_formatter, *, print_error, unavailable_exit):
    try:
        response = request_response()
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    blockers = _filter_blockers_by_session(_collection_blockers(response.data, plural_name), args.session_id)
    if args.json:
        print(json.dumps(blockers, sort_keys=True))
        return 0
    if blockers:
        if len(blockers) > 1:
            print(table_formatter(blockers))
        else:
            print(compact_formatter(blockers[0]))
    return 0


def _handle_blocker_resolution(
    args,
    request_response,
    is_not_found_error,
    not_found_message,
    result_factory,
    compact_formatter,
    *,
    print_error,
    unavailable_exit,
    noinput_exit,
):
    try:
        response = request_response()
    except OpenCodeApiError as error:
        if is_not_found_error(error):
            print_error(not_found_message)
            return noinput_exit
        print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    result = result_factory(response)
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    print(compact_formatter(result))
    return 0


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


def _format_permission_compact(permission):
    fields = [
        ("id", _first_present(permission, "id", "requestID", "requestId")),
        ("session", _blocker_session_id(permission)),
        ("permission", permission.get("permission")),
        ("patterns", _compact_list(permission.get("patterns"))),
        ("always", _compact_list(permission.get("always"))),
        ("tool", _tool_ref(permission.get("tool"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_permission_table(permissions):
    rows = []
    for permission in permissions:
        rows.append(
            [
                _first_present(permission, "id", "requestID", "requestId"),
                _blocker_session_id(permission),
                permission.get("permission"),
                _compact_list(permission.get("patterns")),
                _compact_list(permission.get("always")),
                _tool_ref(permission.get("tool")),
            ]
        )
    return _format_table(["id", "session", "permission", "patterns", "always", "tool"], rows)


def _format_permission_reply_compact(result):
    fields = [("id", result["id"]), ("reply", result["reply"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_question_compact(question):
    question_items = _question_items(question)
    fields = [
        ("id", _first_present(question, "id", "requestID", "requestId")),
        ("session", _blocker_session_id(question)),
        ("questions", len(question_items)),
        ("headers", _compact_list(item.get("header") for item in question_items if isinstance(item, dict))),
        ("question", _first_question_text(question_items)),
        ("tool", _tool_ref(question.get("tool"))),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_question_table(questions):
    rows = []
    for question in questions:
        question_items = _question_items(question)
        rows.append(
            [
                _first_present(question, "id", "requestID", "requestId"),
                _blocker_session_id(question),
                len(question_items),
                _compact_list(item.get("header") for item in question_items if isinstance(item, dict)),
                _first_question_text(question_items),
                _tool_ref(question.get("tool")),
            ]
        )
    return _format_table(["id", "session", "questions", "headers", "question", "tool"], rows)


def _format_question_resolution_compact(result):
    fields = [("id", result["id"]), ("action", result["action"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _filter_blockers_by_session(blockers, session_id):
    if session_id is None:
        return blockers
    return [blocker for blocker in blockers if _blocker_session_id(blocker) == session_id]


def _question_items(question):
    items = question.get("questions")
    return items if isinstance(items, list) else []


def _question_answers_from_args(args):
    if args.answers_json is not None:
        if args.answers:
            raise ValueError("cannot combine positional answers with --answers-json")
        try:
            answers = json.loads(args.answers_json)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid --answers-json: {error}") from error
        if not _valid_question_answers(answers):
            raise ValueError("--answers-json must be a JSON array of string arrays")
        return answers
    if not args.answers:
        raise ValueError("at least one answer is required")
    return [[answer] for answer in args.answers]


def _valid_question_answers(answers):
    return isinstance(answers, list) and all(
        isinstance(answer, list) and all(isinstance(value, str) for value in answer) for answer in answers
    )


def _first_question_text(question_items):
    for item in question_items:
        if isinstance(item, dict) and item.get("question"):
            return item.get("question")
    return None


def _tool_ref(tool):
    if not isinstance(tool, dict):
        return None
    message_id = _first_present(tool, "messageID", "messageId", "message_id")
    call_id = _first_present(tool, "callID", "callId", "call_id")
    if message_id and call_id:
        return f"{message_id}/{call_id}"
    return call_id or message_id
