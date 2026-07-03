import argparse
import json
import os
import signal
import sys
import uuid
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import (
    detect_capabilities,
    format_compact,
    legacy_run_reply_supported,
    unsupported_reasons,
)
from opencode_session.events import format_watch_event, is_abort_event, is_terminal_event, normalize_event
from opencode_session.run_store import RunStore, RunStoreError, default_store_root, format_run_compact


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_DATAERR = 65
EX_NOINPUT = 66
EX_TIMEOUT = 124


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if argv and argv[0] == "run" and "--store" in argv[1:]:
        return _handle_run_store_command(_parse_run_store_args(argv[1:]))

    parser = argparse.ArgumentParser(prog="opencode-session")
    subparsers = parser.add_subparsers(dest="command")

    capabilities_parser = subparsers.add_parser("capabilities")
    _add_server_argument(capabilities_parser)
    capabilities_parser.add_argument("--json", action="store_true", help="print full JSON capability data")

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("directory", help="target directory for the new session")
    create_parser.add_argument("--agent", help="agent name for the new session")
    create_parser.add_argument("--model", help="model name for the new session")
    _add_server_argument(create_parser)
    _add_output_arguments(create_parser)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--directory", help="only show sessions for this target directory")
    list_parser.add_argument("--agent", help="only show sessions for this agent")
    list_parser.add_argument("--model", help="only show sessions for this model")
    list_parser.add_argument("--blockers", action="store_true", help="include pending permission/question counts")
    _add_server_argument(list_parser)
    _add_output_arguments(list_parser)

    for name in ("inspect", "get"):
        inspect_parser = subparsers.add_parser(name)
        inspect_parser.add_argument("session_id", help="session ID to inspect")
        inspect_parser.add_argument("--blockers", action="store_true", help="include pending permission/question counts")
        _add_server_argument(inspect_parser)
        _add_output_arguments(inspect_parser)

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("session_id", help="session ID to delete")
    _add_server_argument(delete_parser)
    _add_output_arguments(delete_parser)

    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument("session_id", help="session ID to abort")
    _add_server_argument(abort_parser)
    _add_output_arguments(abort_parser)

    fork_parser = subparsers.add_parser("fork")
    fork_parser.add_argument("session_id", help="session ID to fork")
    fork_parser.add_argument("--message-id", help="message ID to fork from")
    _add_server_argument(fork_parser)
    _add_output_arguments(fork_parser)

    children_parser = subparsers.add_parser("children")
    children_parser.add_argument("session_id", help="parent session ID")
    children_parser.add_argument("--directory", help="only show child sessions for this target directory")
    _add_server_argument(children_parser)
    _add_output_arguments(children_parser)

    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("session_id", help="session ID to watch")
    _add_server_argument(watch_parser)
    watch_parser.add_argument("--json", action="store_true", help="print normalized event JSON lines")
    watch_parser.add_argument("--timeout", type=_positive_float, help="stop watching after this many seconds")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("prompt", nargs="*", help="prompt text; stdin is used when omitted")
    run_parser.add_argument("--session", help="existing session ID to run in")
    run_parser.add_argument("--directory", help="target directory when creating a disposable session")
    run_parser.add_argument("--agent", help="agent name for a disposable session")
    run_parser.add_argument("--model", help="model name for a disposable session")
    _add_server_argument(run_parser)
    run_parser.add_argument("--json", action="store_true", help="print normalized JSON result")

    steer_parser = subparsers.add_parser("steer")
    _add_admission_arguments(steer_parser)

    queue_parser = subparsers.add_parser("queue")
    _add_admission_arguments(queue_parser)

    permission_parser = subparsers.add_parser("permission")
    permission_subparsers = permission_parser.add_subparsers(dest="permission_command")
    permission_list_parser = permission_subparsers.add_parser("list")
    permission_list_parser.add_argument("--session", dest="session_id", help="only show requests for this session")
    _add_server_argument(permission_list_parser)
    _add_output_arguments(permission_list_parser)
    permission_reply_parser = permission_subparsers.add_parser("reply")
    permission_reply_parser.add_argument("request_id", help="permission request ID to resolve")
    permission_reply_parser.add_argument("reply", choices=("once", "always", "reject"), help="permission response")
    permission_reply_parser.add_argument("--message", help="feedback to send with a rejected permission")
    _add_server_argument(permission_reply_parser)
    _add_output_arguments(permission_reply_parser)

    question_parser = subparsers.add_parser("question")
    question_subparsers = question_parser.add_subparsers(dest="question_command")
    question_list_parser = question_subparsers.add_parser("list")
    question_list_parser.add_argument("--session", dest="session_id", help="only show requests for this session")
    _add_server_argument(question_list_parser)
    _add_output_arguments(question_list_parser)
    question_answer_parser = question_subparsers.add_parser("answer")
    question_answer_parser.add_argument("request_id", help="question request ID to answer")
    question_answer_parser.add_argument("answers", nargs="*", help="answer label/text; repeat for multiple questions")
    question_answer_parser.add_argument("--answers-json", help="JSON array of answer arrays for multi-select questions")
    _add_server_argument(question_answer_parser)
    _add_output_arguments(question_answer_parser)
    question_reject_parser = question_subparsers.add_parser("reject")
    question_reject_parser.add_argument("request_id", help="question request ID to reject")
    _add_server_argument(question_reject_parser)
    _add_output_arguments(question_reject_parser)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 64
    if args.command == "permission" and not args.permission_command:
        permission_parser.print_help(sys.stderr)
        return 64
    if args.command == "question" and not args.question_command:
        question_parser.print_help(sys.stderr)
        return 64

    client = OpenCodeApiClient(args.server)
    if args.command == "permission":
        if args.permission_command == "list":
            try:
                response = client.list_permissions_response()
            except OpenCodeApiError as error:
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            permissions = _filter_blockers_by_session(_collection_blockers(response.data, "permissions"), args.session_id)
            if args.json:
                print(json.dumps(permissions, sort_keys=True))
                return 0
            if permissions:
                print("\n".join(_format_permission_compact(permission) for permission in permissions))
            return 0
        if args.permission_command == "reply":
            try:
                response = client.reply_permission_response(args.request_id, args.reply, message=args.message)
            except OpenCodeApiError as error:
                if _is_permission_request_not_found_error(error, args.request_id):
                    print(f"opencode-session: permission request not found: {args.request_id}", file=sys.stderr)
                    return EX_NOINPUT
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            result = {"id": args.request_id, "reply": args.reply, "ok": bool(response.data), "response": response.data}
            if args.json:
                print(json.dumps(result, sort_keys=True))
                return 0
            print(_format_permission_reply_compact(result))
            return 0

    if args.command == "question":
        if args.question_command == "list":
            try:
                response = client.list_questions_response()
            except OpenCodeApiError as error:
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            questions = _filter_blockers_by_session(_collection_blockers(response.data, "questions"), args.session_id)
            if args.json:
                print(json.dumps(questions, sort_keys=True))
                return 0
            if questions:
                print("\n".join(_format_question_compact(question) for question in questions))
            return 0
        if args.question_command == "answer":
            try:
                answers = _question_answers_from_args(args)
            except ValueError as error:
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_DATAERR
            try:
                response = client.answer_question_response(args.request_id, answers)
            except OpenCodeApiError as error:
                if _is_question_request_not_found_error(error, args.request_id):
                    print(f"opencode-session: question request not found: {args.request_id}", file=sys.stderr)
                    return EX_NOINPUT
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            result = {
                "id": args.request_id,
                "action": "answer",
                "ok": bool(response.data),
                "response": response.data,
                "answers": answers,
            }
            if args.json:
                print(json.dumps(result, sort_keys=True))
                return 0
            print(_format_question_resolution_compact(result))
            return 0
        if args.question_command == "reject":
            try:
                response = client.reject_question_response(args.request_id)
            except OpenCodeApiError as error:
                if _is_question_request_not_found_error(error, args.request_id):
                    print(f"opencode-session: question request not found: {args.request_id}", file=sys.stderr)
                    return EX_NOINPUT
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            result = {"id": args.request_id, "action": "reject", "ok": bool(response.data), "response": response.data}
            if args.json:
                print(json.dumps(result, sort_keys=True))
                return 0
            print(_format_question_resolution_compact(result))
            return 0

    if args.command == "run":
        prompt = _read_prompt(args.prompt)
        session_id = args.session
        created_session_id = None
        try:
            if not legacy_run_reply_supported(client.require_openapi_doc()):
                print(
                    "opencode-session: unsupported route behavior: missing legacy POST "
                    "/session/{sessionID}/run + POST /session/{sessionID}/reply; "
                    "v2 prompt admission is not execution",
                    file=sys.stderr,
                )
                return EX_UNSUPPORTED
            if session_id is None:
                directory = str(Path(args.directory or ".").resolve())
                create_response = client.create_session_response(
                    directory,
                    agent=args.agent,
                    model=args.model,
                )
                session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
                created_session_id = session_id
            run_response = client.run_session_response(session_id, prompt)
            provider_error = _provider_failure(run_response.data)
            if provider_error:
                cleanup_error = _delete_disposable_session(client, created_session_id)
                if cleanup_error:
                    _print_cleanup_error(cleanup_error)
                print(f"opencode-session: provider failure: {provider_error}", file=sys.stderr)
                return EX_UNAVAILABLE
            reply_response = client.reply_session_response(session_id)
            provider_error = _provider_failure(reply_response.data)
            if provider_error:
                cleanup_error = _delete_disposable_session(client, created_session_id)
                if cleanup_error:
                    _print_cleanup_error(cleanup_error)
                print(f"opencode-session: provider failure: {provider_error}", file=sys.stderr)
                return EX_UNAVAILABLE
        except OpenCodeApiError as error:
            cleanup_error = _delete_disposable_session(client, created_session_id)
            if cleanup_error:
                _print_cleanup_error(cleanup_error)
            if session_id is not None and _is_session_not_found_error(error):
                print(f"opencode-session: session not found: {session_id}", file=sys.stderr)
            else:
                print(f"opencode-session: api failure: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if cleanup_error:
            _print_cleanup_error(cleanup_error)
            return EX_UNAVAILABLE
        result = _run_result(session_id, run_response.data, reply_response.data)
        if args.json:
            print(json.dumps(result, sort_keys=True))
            return 0
        print(_format_run_compact(result))
        return 0

    if args.command == "create":
        directory = str(Path(args.directory).resolve())
        try:
            response = client.create_session_response(directory, agent=args.agent, model=args.model)
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        session = response.data
        if args.json:
            print(json.dumps(session, sort_keys=True))
            return 0
        print(_format_session_compact(session))
        return 0

    if args.command == "list":
        try:
            response = client.list_sessions_response()
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        collection = response.data
        directory = str(Path(args.directory).resolve()) if args.directory else None
        sessions = _filter_sessions(_collection_sessions(collection), directory=directory, agent=args.agent, model=args.model)
        blocker_counts = None
        if args.blockers:
            try:
                blocker_counts = _load_blocker_counts(client)
            except OpenCodeApiError as error:
                print(f"opencode-session: blocker summary failed: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
        if args.json:
            if blocker_counts is not None:
                sessions = [_session_with_blocker_counts(session, blocker_counts) for session in sessions]
            print(json.dumps(sessions, sort_keys=True))
            return 0
        if sessions:
            print("\n".join(_format_session_compact(session, _counts_for_session(blocker_counts, session)) for session in sessions))
        return 0

    if args.command in ("inspect", "get"):
        try:
            response = client.get_session_response(args.session_id)
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        session = response.data
        blocker_counts = None
        if args.blockers:
            try:
                blocker_counts = _load_blocker_counts(client)
            except OpenCodeApiError as error:
                print(f"opencode-session: blocker summary failed: {error}", file=sys.stderr)
                return EX_UNAVAILABLE
        if args.json:
            if blocker_counts is not None:
                session = _session_with_blocker_counts(session, blocker_counts)
            print(json.dumps(session, sort_keys=True))
            return 0
        print(_format_session_compact(session, _counts_for_session(blocker_counts, session)))
        return 0

    if args.command == "delete":
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
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        print(f"opencode-session: delete verification failed; session {args.session_id} is still readable", file=sys.stderr)
        return EX_UNAVAILABLE

    if args.command == "abort":
        try:
            response = client.abort_session_response(args.session_id)
        except OpenCodeApiError as error:
            if _is_session_not_found_error(error):
                print(f"opencode-session: session not found: {args.session_id}", file=sys.stderr)
            else:
                print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        abort = _abort_record(args.session_id, response.data)
        if args.json:
            print(json.dumps(abort, sort_keys=True))
        else:
            print(_format_abort_compact(abort))
        return 0

    if args.command == "fork":
        try:
            response = client.fork_session_response(args.session_id, message_id=args.message_id)
        except OpenCodeApiError as error:
            if _is_session_not_found_error(error):
                print(f"opencode-session: session not found: {args.session_id}", file=sys.stderr)
            else:
                print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        fork = _fork_record(args.session_id, args.message_id, response.data)
        if args.json:
            print(json.dumps(fork, sort_keys=True))
        else:
            print(_format_fork_compact(fork))
        return 0

    if args.command == "children":
        try:
            response = client.list_child_sessions_response(args.session_id)
        except OpenCodeApiError as error:
            if _is_session_not_found_error(error):
                print(f"opencode-session: session not found: {args.session_id}", file=sys.stderr)
            else:
                print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        directory = str(Path(args.directory).resolve()) if args.directory else None
        children = _filter_sessions(_collection_sessions(response.data), directory=directory)
        if args.json:
            print(json.dumps(children, sort_keys=True))
        elif children:
            print("\n".join(_format_session_compact(session) for session in children))
        return 0

    if args.command == "watch":
        return _watch_session(args, client)

    if args.command == "steer":
        return _admit_prompt(args, client, "steer")

    if args.command == "queue":
        return _admit_prompt(args, client, "queue")

    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        return EX_UNAVAILABLE

    reasons = unsupported_reasons(capabilities)
    if reasons:
        print(f"opencode-session: unsupported OpenCode server; {'; '.join(reasons)}", file=sys.stderr)
        return EX_UNSUPPORTED

    if args.json:
        print(json.dumps(capabilities, sort_keys=True))
    else:
        print(format_compact(capabilities))
    return 0


def _parse_run_store_args(argv):
    parser = argparse.ArgumentParser(prog="opencode-session run")
    parser.add_argument("--store", default=default_store_root(), help="local orchestration run store directory")
    run_subparsers = parser.add_subparsers(dest="run_command")
    run_subparsers.required = True

    run_init_parser = run_subparsers.add_parser("init")
    run_init_parser.add_argument("name", help="local run name")
    run_init_parser.add_argument("--directory", default=".", help="target directory for the run")
    _add_server_argument(run_init_parser)

    run_status_parser = run_subparsers.add_parser("status")
    run_status_parser.add_argument("name", help="local run name")
    run_status_parser.add_argument("--json", action="store_true", help="print complete run JSON data")

    run_worker_parser = run_subparsers.add_parser("worker")
    run_worker_parser.add_argument("name", help="local run name")
    run_worker_parser.add_argument("worker_id", help="worker record ID")
    run_worker_parser.add_argument("--role", help="worker role")
    run_worker_parser.add_argument("--session", dest="session_id", help="OpenCode session ID reference")
    run_worker_parser.add_argument("--agent", help="agent metadata")
    run_worker_parser.add_argument("--model", help="model metadata")
    run_worker_parser.add_argument("--depends-on", dest="dependencies", action="append", help="worker dependency ID")
    run_worker_parser.add_argument("--prompt-id", dest="prompt_ids", action="append", help="prompt admission ID")
    run_worker_parser.add_argument("--status", help="worker status")
    run_worker_parser.add_argument("--retry-count", type=int, help="worker retry count")
    run_worker_parser.add_argument("--timeout-seconds", type=int, help="worker timeout in seconds")
    run_worker_parser.add_argument("--blocker", dest="blockers", action="append", help="blocker reference")
    run_worker_parser.add_argument("--output-ref", dest="output_refs", action="append", help="output reference")
    return parser.parse_args(argv)


def _handle_run_store_command(args):
    store = RunStore(args.store)
    try:
        if args.run_command == "init":
            run = store.create_run(args.name, directory=args.directory, server_url=args.server)
            print(format_run_compact(run))
            return 0
        if args.run_command == "status":
            run = store.load_run(args.name)
            if args.json:
                print(json.dumps(run, sort_keys=True))
                return 0
            print(format_run_compact(run))
            return 0
        if args.run_command == "worker":
            run = store.upsert_worker(
                args.name,
                args.worker_id,
                role=args.role,
                session_id=args.session_id,
                agent=args.agent,
                model=args.model,
                dependencies=args.dependencies,
                prompt_ids=args.prompt_ids,
                status=args.status,
                retry_count=args.retry_count,
                timeout_seconds=args.timeout_seconds,
                blockers=args.blockers,
                output_refs=args.output_refs,
            )
            print(format_run_compact(run))
            return 0
    except RunStoreError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        if error.kind == "missing":
            return EX_NOINPUT
        return EX_DATAERR
    return 64


def _add_server_argument(parser):
    parser.add_argument(
        "--server",
        default=os.environ.get("OPENCODE_SERVER_URL")
        or os.environ.get("OPENCODE_SERVER")
        or DEFAULT_SERVER_URL,
        help="OpenCode server URL",
    )


def _add_output_arguments(parser):
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print complete JSON data")
    output.add_argument("--raw", action="store_true", help="print raw API response body")


def _add_admission_arguments(parser):
    parser.add_argument("session_id", help="session ID to admit input to")
    parser.add_argument("text", help="prompt text to admit")
    parser.add_argument("--message-id", help="client-supplied prompt/message ID for idempotent admission")
    _add_server_argument(parser)
    _add_output_arguments(parser)


def _admit_prompt(args, client, delivery):
    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        return EX_UNAVAILABLE

    if not capabilities["v2_prompt_support"]:
        print(
            "opencode-session: unsupported v2 prompt capability; durable prompt admission requires "
            "POST /api/session/{sessionID}/prompt or POST /session/{sessionID}/prompt_async; "
            "legacy run/reply fallback is not used for steer/queue admission",
            file=sys.stderr,
        )
        return EX_UNSUPPORTED

    message_id = args.message_id or f"msg_{uuid.uuid4().hex}"
    payload = {
        "messageID": message_id,
        "parts": [{"type": "text", "text": args.text}],
        "delivery": delivery,
    }

    try:
        response = client.admit_prompt_response(
            args.session_id,
            payload,
            capabilities["route_availability"]["v2_prompt"]["path"],
        )
    except OpenCodeApiError as error:
        if _is_idempotent_admission_replay(error, message_id):
            if args.raw:
                _write_raw(error.body or "")
                return 0
            admission = _admission_record(args.session_id, delivery, message_id, error.data)
            if args.json:
                print(json.dumps(admission, sort_keys=True))
            else:
                print(_format_admission_compact(admission))
            return 0
        if error.status in {400, 404, 405, 415, 422}:
            print(
                f"opencode-session: unsupported v2 prompt behavior; {_api_error_detail(error)}; "
                "legacy run/reply fallback is not used",
                file=sys.stderr,
            )
            return EX_UNSUPPORTED
        print(
            f"opencode-session: prompt admission failed; {error}; legacy run/reply fallback is not used",
            file=sys.stderr,
        )
        return EX_UNAVAILABLE

    if args.raw:
        _write_raw(response.body)
        return 0

    admission = _admission_record(args.session_id, delivery, message_id, response.data)
    if args.json:
        print(json.dumps(admission, sort_keys=True))
    else:
        print(_format_admission_compact(admission))
    return 0


def _watch_session(args, client):
    pending_text = None

    def flush_pending_text():
        nonlocal pending_text
        if pending_text is not None:
            print(format_watch_event(pending_text), flush=True)
            pending_text = None

    def emit_event(event):
        nonlocal pending_text
        if args.json:
            print(json.dumps(event, sort_keys=True), flush=True)
            return
        if event["kind"] == "text":
            if pending_text is not None and _same_watch_text_group(pending_text, event):
                pending_text = dict(pending_text)
                pending_text["text"] = (pending_text.get("text") or "") + (event.get("text") or "")
            else:
                flush_pending_text()
                pending_text = dict(event)
            return
        flush_pending_text()
        print(format_watch_event(event), flush=True)

    try:
        with _watch_deadline(args.timeout):
            try:
                capabilities = detect_capabilities(client)
            except OpenCodeApiError as error:
                print(f"opencode-session: {error}", file=sys.stderr)
                return EX_UNAVAILABLE

            event_route = capabilities["route_availability"]["events"]
            if not event_route["available"]:
                print(
                    "opencode-session: unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event",
                    file=sys.stderr,
                )
                return EX_UNSUPPORTED

            try:
                for raw_event in client.stream_events(event_route["path"]):
                    event = normalize_event(raw_event, args.session_id)
                    if event is None:
                        continue
                    emit_event(event)
                    if is_terminal_event(event):
                        flush_pending_text()
                        if is_abort_event(event):
                            return 130
                        return 0
            except OpenCodeApiError as error:
                flush_pending_text()
                print(f"opencode-session: event stream failure: {error}", file=sys.stderr)
                if _is_invalid_event_stream(error):
                    return EX_DATAERR
                return EX_UNAVAILABLE
            flush_pending_text()
            return 0
    except _WatchTimeout:
        flush_pending_text()
        print(f"opencode-session: watch timed out after {_format_timeout(args.timeout)}s", file=sys.stderr)
        return EX_TIMEOUT


def _same_watch_text_group(left, right):
    return left.get("session_id") == right.get("session_id") and left.get("message_id") == right.get("message_id")


def _is_invalid_event_stream(error):
    return isinstance(error.data, dict) and error.data.get("kind") == "invalid_event_stream"


class _WatchTimeout(Exception):
    pass


class _watch_deadline:
    def __init__(self, timeout):
        self.timeout = timeout
        self.previous_handler = None

    def __enter__(self):
        if self.timeout is None:
            return self
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _raise_watch_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.timeout)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.timeout is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous_handler)
        return False


def _raise_watch_timeout(signum, frame):
    raise _WatchTimeout()


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _format_timeout(timeout):
    return str(timeout)


def _write_raw(body):
    sys.stdout.write(body)


def _read_prompt(prompt_words):
    if prompt_words:
        return " ".join(prompt_words)
    prompt = sys.stdin.read()
    if prompt.endswith("\n"):
        prompt = prompt[:-1]
    if prompt.endswith("\r"):
        prompt = prompt[:-1]
    return prompt


def _run_result(session_id, run_message, reply_message):
    return {
        "session_id": session_id,
        "message_ids": {
            "user": _message_value(run_message, "id", "messageID", "messageId"),
            "assistant": _message_value(reply_message, "id", "messageID", "messageId"),
        },
        "status": _message_value(reply_message, "status") or "completed",
        "cost": _message_value(reply_message, "cost"),
        "tokens": _message_tokens(reply_message),
        "text": _message_text(reply_message),
    }


def _format_run_compact(result):
    fields = [
        ("session", result["session_id"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("status", result["status"]),
        ("cost", result["cost"]),
        ("tokens", _tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _message_value(message, *names):
    for name in names:
        value = message.get(name)
        if value is not None:
            return value
    info = message.get("info")
    if isinstance(info, dict):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return None


def _message_tokens(message):
    tokens = _message_value(message, "tokens", "usage")
    return tokens


def _tokens_total(tokens):
    if isinstance(tokens, dict):
        if tokens.get("total") is not None:
            return tokens["total"]
        return sum(value for value in tokens.values() if isinstance(value, int))
    return tokens


def _message_text(message):
    text = _message_value(message, "text", "content")
    if text is not None:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _print_cleanup_error(error):
    print(f"opencode-session: api failure: disposable session cleanup failed: {error}", file=sys.stderr)


def _is_session_not_found_error(error):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    parts = path.split("/")
    if method == "POST" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] in {"run", "reply", "abort", "fork"}
    if method == "GET" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] == "children"
    return method in {"GET", "DELETE"} and len(parts) == 4 and parts[1:3] == ["api", "session"] and bool(parts[3])


def _is_permission_request_not_found_error(error, request_id):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    return method == "POST" and path == f"/permission/{request_id}/reply"


def _is_question_request_not_found_error(error, request_id):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    return method == "POST" and path in {f"/question/{request_id}/reply", f"/question/{request_id}/reject"}


def _delete_disposable_session(client, session_id):
    if session_id is None:
        return None
    try:
        client.delete_session(session_id)
    except OpenCodeApiError as error:
        return error
    return None


def _provider_failure(message):
    status = str(_message_value(message, "status") or "").lower()
    if status not in {"failed", "error", "errored"}:
        return None
    error = _message_value(message, "error", "reason", "message")
    if isinstance(error, dict):
        error = error.get("message") or json.dumps(error, sort_keys=True)
    return error or status


def _format_session_compact(session, blocker_counts=None):
    fields = [
        ("id", _session_value(session, "id", "sessionID", "sessionId")),
        ("title", _session_value(session, "title")),
        ("dir", _session_value(session, "directory", "cwd")),
        ("agent", _session_value(session, "agent")),
        ("model", _session_value(session, "model")),
        ("cost", _session_value(session, "cost")),
        ("tokens", _session_tokens(session)),
        ("created", _session_value(session, "createdAt", "created_at")),
        ("updated", _session_value(session, "updatedAt", "updated_at")),
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


def _format_admission_compact(admission):
    fields = [
        ("state", admission["state"]),
        ("session", admission["session_id"]),
        ("message", admission["message_id"]),
        ("delivery", admission["delivery"]),
        ("admitted", admission["admitted_sequence"]),
        ("promoted", admission["promoted_sequence"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_abort_compact(abort):
    fields = [
        ("session", abort["session_id"]),
        ("accepted", _compact_bool(abort["accepted"])),
        ("status", abort["status"]),
    ]
    return "abort " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_fork_compact(fork):
    fields = [
        ("parent", fork["parent_session_id"]),
        ("child", fork["session_id"]),
        ("message", fork["message_id"]),
    ]
    return "forked " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _abort_record(session_id, data):
    if not isinstance(data, dict):
        data = {}
    status = _first_present(data, "status", "state")
    accepted = _bool_value(_first_present(data, "accepted", "aborted", "ok", "success"))
    if accepted is None and str(status or "").lower() in {"accepted", "aborting", "abort", "aborted", "cancelled", "canceled"}:
        accepted = True
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id", "id") or session_id,
        "accepted": accepted if accepted is not None else True,
        "status": status,
        "response": data,
    }


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


def _format_question_resolution_compact(result):
    fields = [("id", result["id"]), ("action", result["action"]), ("ok", _compact_bool(result["ok"]))]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _admission_record(session_id, delivery, message_id, data):
    if not isinstance(data, dict):
        data = {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id")
        or _first_present(info, "sessionID", "sessionId", "session_id")
        or session_id,
        "message_id": _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
        or message_id,
        "delivery": _first_present(data, "delivery", "deliveryMode", "mode") or delivery,
        "state": _first_present(data, "state", "status", "phase") or "admitted",
        "admitted_sequence": _first_present(data, "admittedSequence", "admitted_sequence", "sequence"),
        "promoted_sequence": _first_present(data, "promotedSequence", "promoted_sequence"),
    }


def _is_idempotent_admission_replay(error, message_id):
    if error.status != 409 or not isinstance(error.data, dict):
        return False
    data = error.data
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    response_message_id = (
        _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
    )
    if response_message_id != message_id:
        return False
    state = _first_present(data, "state", "status", "phase")
    idempotency = _first_present(data, "idempotency", "idempotencyStatus")
    return (
        data.get("duplicate") is True
        or data.get("idempotent") is True
        or idempotency in {"duplicate", "replayed", "existing"}
        or state in {"admitted", "promoted", "running", "completed", "failed"}
    )


def _api_error_detail(error):
    if isinstance(error.data, dict):
        for name in ("error", "message", "detail"):
            value = error.data.get(name)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = _first_present(value, "message", "detail", "error")
                if isinstance(nested, str):
                    return nested
    return str(error)


def _collection_sessions(collection):
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        for name in ("sessions", "children", "data"):
            sessions = collection.get(name)
            if isinstance(sessions, list):
                return sessions
    return []


def _collection_blockers(collection, plural_name):
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        for name in (plural_name, "requests", "data"):
            blockers = collection.get(name)
            if isinstance(blockers, list):
                return blockers
    return []


def _filter_blockers_by_session(blockers, session_id):
    if session_id is None:
        return blockers
    return [blocker for blocker in blockers if _blocker_session_id(blocker) == session_id]


def _blocker_session_id(blocker):
    return _first_present(blocker, "sessionID", "sessionId", "session_id")


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


def _filter_sessions(sessions, *, directory=None, agent=None, model=None):
    filtered = []
    for session in sessions:
        if directory is not None and _session_value(session, "directory", "cwd") != directory:
            continue
        if agent is not None and _session_value(session, "agent") != agent:
            continue
        if model is not None and _session_value(session, "model") != model:
            continue
        filtered.append(session)
    return filtered


def _load_blocker_counts(client):
    permission_response = client.list_permissions_response()
    question_response = client.list_questions_response()
    counts = {}
    for permission in _collection_blockers(permission_response.data, "permissions"):
        _increment_blocker_count(counts, _blocker_session_id(permission), "permissions")
    for question in _collection_blockers(question_response.data, "questions"):
        _increment_blocker_count(counts, _blocker_session_id(question), "questions")
    return counts


def _increment_blocker_count(counts, session_id, name):
    if not session_id:
        return
    session_counts = counts.setdefault(session_id, {"permissions": 0, "questions": 0})
    session_counts[name] += 1


def _counts_for_session(counts, session):
    if counts is None:
        return None
    session_id = _session_value(session, "id", "sessionID", "sessionId")
    session_counts = counts.get(session_id, {})
    permissions = session_counts.get("permissions", 0)
    questions = session_counts.get("questions", 0)
    return {"permissions": permissions, "questions": questions, "total": permissions + questions}


def _session_with_blocker_counts(session, counts):
    augmented = dict(session)
    augmented["blockers"] = _counts_for_session(counts, session)
    return augmented


def _session_value(session, *names):
    for name in names:
        value = session.get(name)
        if value is not None:
            return value
    return None


def _first_present(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _session_tokens(session):
    tokens = session.get("tokens")
    if isinstance(tokens, dict):
        if tokens.get("total") is not None:
            return tokens["total"]
        return sum(value for value in tokens.values() if isinstance(value, int))
    return tokens


def _compact_list(values):
    if not values:
        return None
    return ",".join(str(value) for value in values)


def _compact_value(value):
    if value is None or value == "":
        return "-"
    text = str(value)
    if any(character.isspace() for character in text):
        return json.dumps(text)
    return text


def _compact_bool(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "1", "accepted", "aborted", "ok", "success"}:
            return True
        if lowered in {"false", "no", "0", "rejected", "failed", "error"}:
            return False
    return None
