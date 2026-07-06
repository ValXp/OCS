from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.commands.rendering import render_command_result
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.session_formatting import format_fork_compact, format_session_compact, format_session_table
from opencode_session.session_lifecycle import format_abort_compact
from opencode_session.session_services import (
    SessionCommandError,
    SessionCommandService,
    counts_for_session,
    session_with_blocker_counts,
)


def add_session_parsers(subparsers, *, add_server_argument, add_output_arguments, handler):
    create_parser = subparsers.add_parser("create", help="create a session")
    create_parser.add_argument("directory", help="target directory for the new session")
    create_parser.add_argument("--agent", help="agent name for the new session")
    create_parser.add_argument("--model", help="model name for the new session")
    add_server_argument(create_parser)
    add_output_arguments(create_parser)
    create_parser.set_defaults(command_handler=handler)

    list_parser = subparsers.add_parser("list", help="list sessions")
    list_parser.add_argument("--directory", help="only show sessions for this target directory")
    list_parser.add_argument("--agent", help="only show sessions for this agent")
    list_parser.add_argument("--model", help="only show sessions for this model")
    list_parser.add_argument("--blockers", action="store_true", help="include permission/question blocker counts")
    add_server_argument(list_parser)
    add_output_arguments(list_parser)
    list_parser.set_defaults(command_handler=handler)

    for name in ("inspect", "get"):
        inspect_parser = subparsers.add_parser(name, help="inspect one session")
        inspect_parser.add_argument("session_id", help="session ID to inspect")
        inspect_parser.add_argument("--blockers", action="store_true", help="include permission/question blocker counts")
        add_server_argument(inspect_parser)
        add_output_arguments(inspect_parser)
        inspect_parser.set_defaults(command_handler=handler)

    delete_parser = subparsers.add_parser("delete", help="delete a session")
    delete_parser.add_argument("session_id", help="session ID to delete")
    add_server_argument(delete_parser)
    add_output_arguments(delete_parser)
    delete_parser.set_defaults(command_handler=handler)

    abort_parser = subparsers.add_parser("abort", help="abort a session")
    abort_parser.add_argument("session_id", help="session ID to abort")
    add_server_argument(abort_parser)
    add_output_arguments(abort_parser)
    abort_parser.set_defaults(command_handler=handler)

    fork_parser = subparsers.add_parser("fork", help="fork a session")
    fork_parser.add_argument("session_id", help="session ID to fork")
    fork_parser.add_argument("--message-id", help="message ID to fork from")
    add_server_argument(fork_parser)
    add_output_arguments(fork_parser)
    fork_parser.set_defaults(command_handler=handler)

    children_parser = subparsers.add_parser("children", help="list child sessions")
    children_parser.add_argument("session_id", help="parent session ID")
    children_parser.add_argument("--directory", help="only show child sessions for this target directory")
    add_server_argument(children_parser)
    add_output_arguments(children_parser)
    children_parser.set_defaults(command_handler=handler)


def handle_session_command(args, *, print_error, unavailable_exit, client_factory=OpenCodeApiClient):
    try:
        service = SessionCommandService(client_factory(args.server))
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

    try:
        handler = _SESSION_HANDLERS.get(args.command)
        if handler is not None:
            return handler(args, service)
    except SessionCommandError as error:
        print_error(str(error))
        return unavailable_exit
    return 64


def _handle_create(args, service):
    result = service.create(args.directory, agent=args.agent, model=args.model)
    return render_command_result(args, result.session, raw_body=result.raw_body, compact=format_session_compact(result.session))


def _handle_list(args, service):
    result = service.list(
        directory=args.directory,
        agent=args.agent,
        model=args.model,
        include_blockers=args.blockers,
    )
    if args.raw:
        return render_command_result(args, raw_body=result.raw_body)
    sessions = result.sessions
    if args.json:
        if result.blocker_counts is not None:
            sessions = [session_with_blocker_counts(session, result.blocker_counts) for session in sessions]
        return render_command_result(args, sessions)
    if sessions:
        if len(sessions) > 1:
            print(format_session_table(sessions, result.blocker_counts))
        else:
            print(format_session_compact(sessions[0], counts_for_session(result.blocker_counts, sessions[0])))
    return 0


def _handle_inspect(args, service):
    result = service.inspect(args.session_id, include_blockers=args.blockers)
    if args.raw:
        return render_command_result(args, raw_body=result.raw_body)
    session = result.session
    if args.json:
        if result.blocker_counts is not None:
            session = session_with_blocker_counts(session, result.blocker_counts)
    return render_command_result(
        args,
        session,
        compact=format_session_compact(session, counts_for_session(result.blocker_counts, session)),
    )


def _handle_delete(args, service):
    result = service.delete(args.session_id)
    data = {
        "deleted": True,
        "id": result.session_id,
        "response": result.response,
        "verified": result.verified,
    }
    return render_command_result(
        args,
        data,
        raw_body=result.raw_body,
        compact=f"deleted id={_compact_value(result.session_id)} verified={result.verified}",
    )


def _handle_abort(args, service):
    result = service.abort(args.session_id)
    return render_command_result(args, result.abort, raw_body=result.raw_body, compact=format_abort_compact(result.abort))


def _handle_fork(args, service):
    result = service.fork(args.session_id, message_id=args.message_id)
    return render_command_result(args, result.fork, raw_body=result.raw_body, compact=format_fork_compact(result.fork))


def _handle_children(args, service):
    result = service.children(args.session_id, directory=args.directory)
    if args.raw:
        return render_command_result(args, raw_body=result.raw_body)
    if args.json:
        return render_command_result(args, result.children)
    elif result.children:
        if len(result.children) > 1:
            print(format_session_table(result.children))
        else:
            print(format_session_compact(result.children[0]))
    return 0


_SESSION_HANDLERS = {
    "create": _handle_create,
    "list": _handle_list,
    "inspect": _handle_inspect,
    "get": _handle_inspect,
    "delete": _handle_delete,
    "abort": _handle_abort,
    "fork": _handle_fork,
    "children": _handle_children,
}
