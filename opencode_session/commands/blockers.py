import json

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocker_formatting import (
    format_permission_compact,
    format_permission_reply_compact,
    format_permission_table,
    format_question_compact,
    format_question_resolution_compact,
    format_question_table,
)
from opencode_session.blocker_services import BlockerCommandError, BlockerCommandService
from opencode_session.formatting import write_raw as _write_raw


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
        service = BlockerCommandService(client_factory(args.server))
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    try:
        if args.command == "permission":
            return handle_permission_command(args, service)
        if args.command == "question":
            return handle_question_command(args, service)
    except BlockerCommandError as error:
        print_error(str(error))
        return _blocker_error_exit(error, unavailable_exit, noinput_exit, dataerr_exit)
    return 64


def handle_permission_command(args, service):
    if args.permission_command == "list":
        result = service.list_permissions(session_id=args.session_id)
        return _print_blocker_list(args, result, format_permission_compact, format_permission_table)
    if args.permission_command == "reply":
        result = service.reply_permission(args.request_id, args.reply, message=args.message)
        return _print_resolution(args, result, format_permission_reply_compact)
    return 64


def handle_question_command(args, service):
    if args.question_command == "list":
        result = service.list_questions(session_id=args.session_id)
        return _print_blocker_list(args, result, format_question_compact, format_question_table)
    if args.question_command == "answer":
        result = service.answer_question(args.request_id, args.answers, answers_json=args.answers_json)
        return _print_resolution(args, result, format_question_resolution_compact)
    if args.question_command == "reject":
        result = service.reject_question(args.request_id)
        return _print_resolution(args, result, format_question_resolution_compact)
    return 64


def _print_blocker_list(args, result, compact_formatter, table_formatter):
    if args.raw:
        _write_raw(result.raw_body)
        return 0
    blockers = result.blockers
    if args.json:
        print(json.dumps(blockers, sort_keys=True))
        return 0
    if blockers:
        if len(blockers) > 1:
            print(table_formatter(blockers))
        else:
            print(compact_formatter(blockers[0]))
    return 0


def _print_resolution(args, result, compact_formatter):
    if args.raw:
        _write_raw(result.raw_body)
        return 0
    if args.json:
        print(json.dumps(result.result, sort_keys=True))
        return 0
    print(compact_formatter(result.result))
    return 0


def _blocker_error_exit(error, unavailable_exit, noinput_exit, dataerr_exit):
    if error.kind == "noinput":
        return noinput_exit
    if error.kind == "dataerr":
        return dataerr_exit
    return unavailable_exit
