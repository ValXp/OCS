import json
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocker_inventory import blocker_counts_for_session, load_blocker_counts
from opencode_session.formatting import (
    compact_value as _compact_value,
    format_table as _format_table,
    write_raw as _write_raw,
)
from opencode_session.records import (
    collection_sessions,
    first_present as _first_present,
    session_record as _session_record,
    tokens_total as _tokens_total,
)
from opencode_session.session_lifecycle import abort_record, format_abort_compact, is_session_not_found_error


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
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

    if args.command == "create":
        return _handle_create(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command == "list":
        return _handle_list(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command in ("inspect", "get"):
        return _handle_inspect(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command == "delete":
        return _handle_delete(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command == "abort":
        return _handle_abort(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command == "fork":
        return _handle_fork(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    if args.command == "children":
        return _handle_children(args, client, print_error=print_error, unavailable_exit=unavailable_exit)
    return 64


def _handle_create(args, client, *, print_error, unavailable_exit):
    directory = str(Path(args.directory).resolve())
    try:
        response = client.create_session_response(directory, agent=args.agent, model=args.model)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    session = response.data
    if args.json:
        print(json.dumps(session, sort_keys=True))
        return 0
    print(_format_session_compact(session))
    return 0


def _handle_list(args, client, *, print_error, unavailable_exit):
    try:
        response = client.list_sessions_response()
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    collection = response.data
    directory = str(Path(args.directory).resolve()) if args.directory else None
    sessions = _filter_sessions(collection_sessions(collection), directory=directory, agent=args.agent, model=args.model)
    blocker_counts = None
    if args.blockers:
        try:
            blocker_counts = load_blocker_counts(client)
        except OpenCodeApiError as error:
            print_error(f"blocker summary failed: {error}")
            return unavailable_exit
    if args.json:
        if blocker_counts is not None:
            sessions = [_session_with_blocker_counts(session, blocker_counts) for session in sessions]
        print(json.dumps(sessions, sort_keys=True))
        return 0
    if sessions:
        if len(sessions) > 1:
            print(_format_session_table(sessions, blocker_counts))
        else:
            print(_format_session_compact(sessions[0], _counts_for_session(blocker_counts, sessions[0])))
    return 0


def _handle_inspect(args, client, *, print_error, unavailable_exit):
    try:
        response = client.get_session_response(args.session_id)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    session = response.data
    blocker_counts = None
    if args.blockers:
        try:
            blocker_counts = load_blocker_counts(client)
        except OpenCodeApiError as error:
            print_error(f"blocker summary failed: {error}")
            return unavailable_exit
    if args.json:
        if blocker_counts is not None:
            session = _session_with_blocker_counts(session, blocker_counts)
        print(json.dumps(session, sort_keys=True))
        return 0
    print(_format_session_compact(session, _counts_for_session(blocker_counts, session)))
    return 0


def _handle_delete(args, client, *, print_error, unavailable_exit):
    delete_response = None
    deleted = False
    try:
        delete_response = client.delete_session_response(args.session_id)
        deleted = True
        client.get_session(args.session_id)
    except OpenCodeApiError as error:
        if deleted and error.status == 404:
            if args.raw:
                _write_raw(delete_response.body if delete_response else "")
                return 0
            if args.json:
                print(
                    json.dumps(
                        {
                            "deleted": True,
                            "id": args.session_id,
                            "response": delete_response.data if delete_response else None,
                            "verified": "unreadable",
                        },
                        sort_keys=True,
                    )
                )
                return 0
            print(f"deleted id={_compact_value(args.session_id)} verified=unreadable")
            return 0
        print_error(str(error))
        return unavailable_exit
    print_error(f"delete verification failed; session {args.session_id} is still readable")
    return unavailable_exit


def _handle_abort(args, client, *, print_error, unavailable_exit):
    try:
        response = client.abort_session_response(args.session_id)
    except OpenCodeApiError as error:
        if is_session_not_found_error(error):
            print_error(f"session not found: {args.session_id}")
        else:
            print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    abort = abort_record(args.session_id, response.data)
    if args.json:
        print(json.dumps(abort, sort_keys=True))
    else:
        print(format_abort_compact(abort))
    return 0


def _handle_fork(args, client, *, print_error, unavailable_exit):
    try:
        response = client.fork_session_response(args.session_id, message_id=args.message_id)
    except OpenCodeApiError as error:
        if is_session_not_found_error(error):
            print_error(f"session not found: {args.session_id}")
        else:
            print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    fork = _fork_record(args.session_id, args.message_id, response.data)
    if args.json:
        print(json.dumps(fork, sort_keys=True))
    else:
        print(_format_fork_compact(fork))
    return 0


def _handle_children(args, client, *, print_error, unavailable_exit):
    try:
        response = client.list_child_sessions_response(args.session_id)
    except OpenCodeApiError as error:
        if is_session_not_found_error(error):
            print_error(f"session not found: {args.session_id}")
        else:
            print_error(str(error))
        return unavailable_exit
    if args.raw:
        _write_raw(response.body)
        return 0
    directory = str(Path(args.directory).resolve()) if args.directory else None
    children = _filter_sessions(collection_sessions(response.data), directory=directory)
    if args.json:
        print(json.dumps(children, sort_keys=True))
    elif children:
        if len(children) > 1:
            print(_format_session_table(children))
        else:
            print(_format_session_compact(children[0]))
    return 0


def _format_session_compact(session, blocker_counts=None):
    session = _session_record(session)
    fields = [
        ("id", session.get("id")),
        ("title", session.get("title")),
        ("dir", session.get("directory")),
        ("agent", session.get("agent")),
        ("model", session.get("model")),
        ("cost", session.get("cost")),
        ("tokens", _session_tokens(session)),
        ("created", session.get("createdAt")),
        ("updated", session.get("updatedAt")),
    ]
    if blocker_counts is not None:
        fields.extend(
            [
                ("permissions", blocker_counts["permissions"]),
                ("questions", blocker_counts["questions"]),
                ("blockers", blocker_counts["total"]),
            ]
        )
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_session_table(sessions, blocker_counts=None):
    headers = ["id", "title", "dir", "agent", "model", "cost", "tokens", "updated"]
    if blocker_counts is not None:
        headers.extend(["permissions", "questions", "blockers"])
    rows = []
    for session in sessions:
        session = _session_record(session)
        row = [
            session.get("id"),
            session.get("title"),
            session.get("directory"),
            session.get("agent"),
            session.get("model"),
            session.get("cost"),
            _session_tokens(session),
            session.get("updatedAt"),
        ]
        if blocker_counts is not None:
            counts = _counts_for_session(blocker_counts, session)
            row.extend([counts["permissions"], counts["questions"], counts["total"]])
        rows.append(row)
    return _format_table(headers, rows)


def _format_fork_compact(fork):
    fields = [
        ("parent", fork["parent_session_id"]),
        ("child", fork["session_id"]),
        ("message", fork["message_id"]),
    ]
    return "forked " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _fork_record(parent_session_id, message_id, data):
    if not isinstance(data, dict):
        data = {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    return {
        "parent_session_id": _first_present(
            data,
            "parentID",
            "parentId",
            "parentSessionID",
            "parentSessionId",
            "parent_session_id",
        )
        or parent_session_id,
        "session_id": _first_present(data, "id", "sessionID", "sessionId", "childSessionID", "childSessionId")
        or _first_present(session, "id", "sessionID", "sessionId"),
        "message_id": _first_present(data, "messageID", "messageId", "message_id") or message_id,
        "response": data,
    }


def _filter_sessions(sessions, *, directory=None, agent=None, model=None):
    filtered = []
    for session in sessions:
        session_record = _session_record(session)
        if directory is not None and session_record.get("directory") != directory:
            continue
        if agent is not None and session_record.get("agent") != agent:
            continue
        if model is not None and session_record.get("model") != model:
            continue
        filtered.append(session)
    return filtered


def _counts_for_session(counts, session):
    if counts is None:
        return None
    return blocker_counts_for_session(counts, _session_record(session).get("id"))


def _session_with_blocker_counts(session, counts):
    augmented = dict(session)
    augmented["blockers"] = _counts_for_session(counts, session)
    return augmented


def _session_tokens(session):
    session = _session_record(session)
    return _tokens_total(session.get("tokens"))
