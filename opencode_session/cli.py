import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import (
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    SESSION_MESSAGE_PATH,
    blocking_message_supported,
    detect_capabilities,
    format_compact,
    legacy_run_reply_supported,
    unsupported_reasons,
)
from opencode_session.events import format_watch_event, is_abort_event, is_terminal_event, normalize_event
from opencode_session.run_store import RunStore, RunStoreError, default_store_root, format_run_compact
from opencode_session.status import short_status


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
CLI_NAME = "ocs"
SMOKE_SESSION_PREFIX = "ocs-smoke-"
SMOKE_EVENT_TIMEOUT_SECONDS = 10.0
BLOCKING_EXECUTION_TIMEOUT_SECONDS = 120
LIVE_VALIDATE_ENV = "OCS_LIVE_VALIDATE"
LIVE_SESSION_PREFIX = "ocs-live-"
LIVE_VALIDATE_PROMPT = "Reply exactly PONG."
LIVE_EVENT_OBSERVATION_TIMEOUT = 1.0
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_DATAERR = 65
EX_NOINPUT = 66
EX_TIMEOUT = 124
EX_PARTIAL = 1
EX_BLOCKED = 75
EX_ABORTED = 130


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if argv and argv[0] == "run" and "--store" in argv[1:]:
        return _handle_run_store_command(_parse_run_store_args(argv[1:]))

    parser = argparse.ArgumentParser(prog=CLI_NAME, description="Agent-friendly OpenCode session CLI.")
    subparsers = parser.add_subparsers(dest="command")

    capabilities_parser = subparsers.add_parser("capabilities", help="probe OpenCode API capabilities")
    _add_server_argument(capabilities_parser)
    capabilities_parser.add_argument("--json", action="store_true", help="print full JSON capability data")

    create_parser = subparsers.add_parser("create", help="create a session")
    create_parser.add_argument("directory", help="target directory for the new session")
    create_parser.add_argument("--agent", help="agent name for the new session")
    create_parser.add_argument("--model", help="model name for the new session")
    _add_server_argument(create_parser)
    _add_output_arguments(create_parser)

    list_parser = subparsers.add_parser("list", help="list sessions")
    list_parser.add_argument("--directory", help="only show sessions for this target directory")
    list_parser.add_argument("--agent", help="only show sessions for this agent")
    list_parser.add_argument("--model", help="only show sessions for this model")
    list_parser.add_argument("--blockers", action="store_true", help="include permission/question blocker counts")
    _add_server_argument(list_parser)
    _add_output_arguments(list_parser)

    for name in ("inspect", "get"):
        inspect_parser = subparsers.add_parser(name, help="inspect one session")
        inspect_parser.add_argument("session_id", help="session ID to inspect")
        inspect_parser.add_argument("--blockers", action="store_true", help="include permission/question blocker counts")
        _add_server_argument(inspect_parser)
        _add_output_arguments(inspect_parser)

    delete_parser = subparsers.add_parser("delete", help="delete a session")
    delete_parser.add_argument("session_id", help="session ID to delete")
    _add_server_argument(delete_parser)
    _add_output_arguments(delete_parser)

    abort_parser = subparsers.add_parser("abort", help="abort a session")
    abort_parser.add_argument("session_id", help="session ID to abort")
    _add_server_argument(abort_parser)
    _add_output_arguments(abort_parser)

    fork_parser = subparsers.add_parser("fork", help="fork a session")
    fork_parser.add_argument("session_id", help="session ID to fork")
    fork_parser.add_argument("--message-id", help="message ID to fork from")
    _add_server_argument(fork_parser)
    _add_output_arguments(fork_parser)

    children_parser = subparsers.add_parser("children", help="list child sessions")
    children_parser.add_argument("session_id", help="parent session ID")
    children_parser.add_argument("--directory", help="only show child sessions for this target directory")
    _add_server_argument(children_parser)
    _add_output_arguments(children_parser)

    watch_parser = subparsers.add_parser("watch", help="watch session progress events")
    watch_parser.add_argument("session_id", help="session ID to watch")
    _add_server_argument(watch_parser)
    watch_parser.add_argument("--json", action="store_true", help="print normalized event JSON lines")
    watch_parser.add_argument("--timeout", type=_positive_float, help="stop watching after this many seconds")

    run_store_parser = subparsers.add_parser("run", help="manage local orchestration runs")
    _add_run_store_arguments(run_store_parser)

    run_parser = subparsers.add_parser(
        "run_blocking",
        help="execute a task and wait for an assistant reply",
        description="Execute a task and wait for an assistant reply or terminal failure.",
    )
    run_parser.add_argument("prompt", nargs="*", help="prompt text; stdin is used when omitted")
    run_parser.add_argument("--session", help="existing session ID to run in")
    run_parser.add_argument("--directory", help="target directory when creating a disposable session")
    run_parser.add_argument("--agent", help="agent name for a disposable session")
    run_parser.add_argument("--model", help="model name for a disposable session")
    _add_server_argument(run_parser)
    run_parser.add_argument("--json", action="store_true", help="print normalized JSON result")

    steer_parser = subparsers.add_parser(
        "steer",
        help="admit durable input to a session",
        description="Admit steer or queue input to a session and report admission/progress state; does not wait for an assistant reply.",
    )
    _add_admission_arguments(steer_parser)

    smoke_parser = subparsers.add_parser("smoke", help="run a deterministic no-live OpenCode smoke test")
    smoke_parser.add_argument("--directory", default=".", help="target directory for disposable smoke sessions")
    smoke_parser.add_argument("--prefix", default=SMOKE_SESSION_PREFIX, help="recognizable disposable session prefix")
    smoke_parser.add_argument(
        "--no-live-model",
        action="store_true",
        default=True,
        help="keep smoke in no-live-model mode; live-provider validation is separate",
    )
    smoke_parser.add_argument(
        "--event-timeout",
        type=_positive_float,
        default=SMOKE_EVENT_TIMEOUT_SECONDS,
        help="event watch timeout in seconds",
    )
    smoke_parser.add_argument("--event-limit", type=_positive_int, default=3, help="maximum matching events to observe")
    _add_server_argument(smoke_parser)
    smoke_parser.add_argument("--json", action="store_true", help="print smoke result JSON")

    live_parser = subparsers.add_parser(
        "live_validate",
        help=f"run opt-in live-provider validation; requires {LIVE_VALIDATE_ENV}=1",
        description=(
            "Run an explicit live-provider validation using the minimal prompt: Reply exactly PONG.\n"
            f"Requires {LIVE_VALIDATE_ENV}=1; expected token use is two minimal PONG prompts at most.\n"
            "Creates disposable sessions and verifies they are deleted before the command exits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    live_parser.add_argument("--directory", default=".", help="target directory for disposable live validation sessions")
    live_parser.add_argument("--prefix", default=LIVE_SESSION_PREFIX, help="recognizable disposable live session prefix")
    live_parser.add_argument("--agent", help="agent name for disposable live validation sessions")
    live_parser.add_argument("--model", help="model name for disposable live validation sessions")
    _add_server_argument(live_parser)
    live_parser.add_argument("--json", action="store_true", help="print live validation result JSON")

    cleanup_parser = subparsers.add_parser("cleanup", help="delete stale disposable smoke sessions")
    cleanup_parser.add_argument("--directory", default=".", help="target directory to clean")
    cleanup_parser.add_argument("--prefix", default=SMOKE_SESSION_PREFIX, help="disposable session prefix to match")
    _add_server_argument(cleanup_parser)
    cleanup_parser.add_argument("--json", action="store_true", help="print cleanup result JSON")

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
    if args.command == "run":
        return _handle_run_store_command(args)

    try:
        client = OpenCodeApiClient(args.server)
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE
    if args.command == "permission":
        if args.permission_command == "list":
            try:
                response = client.list_permissions_response()
            except OpenCodeApiError as error:
                _print_error(str(error))
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            permissions = _filter_blockers_by_session(_collection_blockers(response.data, "permissions"), args.session_id)
            if args.json:
                print(json.dumps(permissions, sort_keys=True))
                return 0
            if permissions:
                if len(permissions) > 1:
                    print(_format_permission_table(permissions))
                else:
                    print(_format_permission_compact(permissions[0]))
            return 0
        if args.permission_command == "reply":
            try:
                response = client.reply_permission_response(args.request_id, args.reply, message=args.message)
            except OpenCodeApiError as error:
                if _is_permission_request_not_found_error(error, args.request_id):
                    _print_error(f"permission request not found: {args.request_id}")
                    return EX_NOINPUT
                _print_error(str(error))
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
                _print_error(str(error))
                return EX_UNAVAILABLE
            if args.raw:
                _write_raw(response.body)
                return 0
            questions = _filter_blockers_by_session(_collection_blockers(response.data, "questions"), args.session_id)
            if args.json:
                print(json.dumps(questions, sort_keys=True))
                return 0
            if questions:
                if len(questions) > 1:
                    print(_format_question_table(questions))
                else:
                    print(_format_question_compact(questions[0]))
            return 0
        if args.question_command == "answer":
            try:
                answers = _question_answers_from_args(args)
            except ValueError as error:
                _print_error(str(error))
                return EX_DATAERR
            try:
                response = client.answer_question_response(args.request_id, answers)
            except OpenCodeApiError as error:
                if _is_question_request_not_found_error(error, args.request_id):
                    _print_error(f"question request not found: {args.request_id}")
                    return EX_NOINPUT
                _print_error(str(error))
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
                    _print_error(f"question request not found: {args.request_id}")
                    return EX_NOINPUT
                _print_error(str(error))
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

    if args.command == "run_blocking":
        prompt = _read_prompt(args.prompt)
        session_id = args.session
        created_session_id = None
        try:
            capabilities = _blocking_execution_capabilities(client.require_openapi_doc())
            if _blocking_execution_strategy(capabilities) is None:
                _print_error(_unsupported_blocking_execution_message())
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
            result = _execute_blocking_prompt(client, session_id, prompt, capabilities)
        except _BlockingProviderFailure as error:
            cleanup_error = _delete_disposable_session(client, created_session_id)
            if cleanup_error:
                _print_cleanup_error(cleanup_error)
            _print_error(f"provider failure: {error}")
            return EX_UNAVAILABLE
        except OpenCodeApiError as error:
            cleanup_error = _delete_disposable_session(client, created_session_id)
            if cleanup_error:
                _print_cleanup_error(cleanup_error)
            if session_id is not None and _is_session_not_found_error(error):
                _print_error(f"session not found: {session_id}")
            else:
                _print_error(f"api failure: {error}")
            return EX_UNAVAILABLE
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if cleanup_error:
            _print_cleanup_error(cleanup_error)
            return EX_UNAVAILABLE
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
            _print_error(str(error))
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
            _print_error(str(error))
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
                _print_error(f"blocker summary failed: {error}")
                return EX_UNAVAILABLE
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

    if args.command in ("inspect", "get"):
        try:
            response = client.get_session_response(args.session_id)
        except OpenCodeApiError as error:
            _print_error(str(error))
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
                _print_error(f"blocker summary failed: {error}")
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
            _print_error(str(error))
            return EX_UNAVAILABLE
        _print_error(f"delete verification failed; session {args.session_id} is still readable")
        return EX_UNAVAILABLE

    if args.command == "abort":
        try:
            response = client.abort_session_response(args.session_id)
        except OpenCodeApiError as error:
            if _is_session_not_found_error(error):
                _print_error(f"session not found: {args.session_id}")
            else:
                _print_error(str(error))
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
                _print_error(f"session not found: {args.session_id}")
            else:
                _print_error(str(error))
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
                _print_error(f"session not found: {args.session_id}")
            else:
                _print_error(str(error))
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        directory = str(Path(args.directory).resolve()) if args.directory else None
        children = _filter_sessions(_collection_sessions(response.data), directory=directory)
        if args.json:
            print(json.dumps(children, sort_keys=True))
        elif children:
            if len(children) > 1:
                print(_format_session_table(children))
            else:
                print(_format_session_compact(children[0]))
        return 0

    if args.command == "watch":
        return _watch_session(args, client)

    if args.command == "steer":
        return _admit_prompt(args, client, args.delivery)

    if args.command == "smoke":
        return _run_smoke(args, client)

    if args.command == "live_validate":
        return _run_live_validate(args, client)

    if args.command == "cleanup":
        return _cleanup_disposable_command(args, client)

    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE

    reasons = unsupported_reasons(capabilities)
    if reasons:
        _print_error(f"unsupported OpenCode server; {'; '.join(reasons)}")
        return EX_UNSUPPORTED

    if args.json:
        print(json.dumps(capabilities, sort_keys=True))
    else:
        print(format_compact(capabilities))
    return 0


def _parse_run_store_args(argv):
    parser = argparse.ArgumentParser(prog=f"{CLI_NAME} run")
    _add_run_store_arguments(parser)
    return parser.parse_args(argv)


def _add_run_store_arguments(parser):
    parser.add_argument("--store", default=default_store_root(), help="local orchestration run store directory")
    run_subparsers = parser.add_subparsers(dest="run_command")
    run_subparsers.required = True

    run_init_parser = run_subparsers.add_parser("init")
    run_init_parser.add_argument("name", help="local run name")
    run_init_parser.add_argument("--directory", default=".", help="target directory for the run")
    _add_server_argument(run_init_parser)

    run_start_parser = run_subparsers.add_parser("start")
    run_start_parser.add_argument("name", help="local run name")
    run_start_parser.add_argument("--prompt", help="prompt text for a single worker; omit to start stored worker prompts")
    run_start_parser.add_argument("--worker", default="worker", help="worker record ID")
    run_start_parser.add_argument("--role", default="worker", help="worker role")
    run_start_parser.add_argument("--directory", help="target directory when creating the run")
    run_start_parser.add_argument("--server", help="OpenCode server URL")
    run_start_parser.add_argument("--session", dest="session_id", help="existing OpenCode session ID to attach")
    run_start_parser.add_argument("--agent", help="agent name when creating a worker session")
    run_start_parser.add_argument("--model", help="model name when creating a worker session")
    run_start_parser.add_argument("--cleanup", action="store_true", help="delete a session created by this start after it reaches done")

    run_status_parser = run_subparsers.add_parser("status")
    run_status_parser.add_argument("name", help="local run name")
    run_status_parser.add_argument("--json", action="store_true", help="print run JSON data")

    run_collect_parser = run_subparsers.add_parser("collect")
    run_collect_parser.add_argument("name", help="local run name")
    run_collect_parser.add_argument("--worker", help="worker record ID")
    run_collect_parser.add_argument("--json", action="store_true", help="print collected result JSON")

    run_worker_parser = run_subparsers.add_parser("worker")
    run_worker_parser.add_argument("name", help="local run name")
    run_worker_parser.add_argument("worker_id", help="worker record ID")
    run_worker_parser.add_argument("--role", help="worker role")
    run_worker_parser.add_argument("--session", dest="session_id", help="OpenCode session ID reference")
    run_worker_parser.add_argument("--agent", help="agent metadata")
    run_worker_parser.add_argument("--model", help="model metadata")
    run_worker_parser.add_argument("--prompt", help="prompt text to run for this worker")
    run_worker_parser.add_argument("--depends-on", dest="dependencies", action="append", help="worker dependency ID")
    run_worker_parser.add_argument("--prompt-id", dest="prompt_ids", action="append", help="prompt admission ID")
    run_worker_parser.add_argument("--status", help="worker status")
    run_worker_parser.add_argument("--retry-count", type=int, help="worker retry count")
    run_worker_parser.add_argument("--retry-limit", type=int, help="maximum automatic retries for retryable failures")
    run_worker_parser.add_argument(
        "--retryable",
        "--retryable-failure",
        dest="retryable_failures",
        action="append",
        choices=("api", "provider", "timeout", "all"),
        help="failure category eligible for automatic retry; repeat for multiple categories",
    )
    run_worker_parser.add_argument("--timeout-seconds", type=int, help="worker timeout in seconds")
    run_worker_parser.add_argument(
        "--timeout-policy",
        choices=("timeout", "blocked", "failed", "aborted"),
        help="status to apply when the worker timeout elapses",
    )
    run_worker_parser.add_argument("--blocker", dest="blockers", action="append", help="blocker reference")
    run_worker_parser.add_argument("--output-ref", dest="output_refs", action="append", help="output reference")

    run_steer_parser = run_subparsers.add_parser("steer")
    run_steer_parser.add_argument("name", help="local run name")
    run_steer_parser.add_argument("worker_id", help="worker record ID")
    run_steer_parser.add_argument("text", help="input text to admit to the worker session")
    run_steer_parser.add_argument("--delivery", choices=("steer", "queue"), default="steer", help="admission delivery mode")
    run_steer_parser.add_argument("--message-id", help="client-supplied prompt/message ID for idempotent admission")
    run_steer_parser.add_argument("--json", action="store_true", help="print run-scoped admission JSON")

    run_abort_parser = run_subparsers.add_parser("abort")
    run_abort_parser.add_argument("name", help="local run name")
    run_abort_parser.add_argument("worker_id", help="worker record ID")
    run_abort_parser.add_argument("--json", action="store_true", help="print run-scoped abort JSON")


def _handle_run_store_command(args):
    store = RunStore(args.store)
    try:
        if args.run_command == "start":
            return _start_orchestration_run(args, store)
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
        if args.run_command == "collect":
            return _collect_run_results(args, store)
        if args.run_command == "worker":
            run = store.upsert_worker(
                args.name,
                args.worker_id,
                role=args.role,
                session_id=args.session_id,
                agent=args.agent,
                model=args.model,
                prompt=args.prompt,
                dependencies=args.dependencies,
                prompt_ids=args.prompt_ids,
                status=args.status,
                retry_count=args.retry_count,
                retry_limit=args.retry_limit,
                retryable_failures=args.retryable_failures,
                timeout_seconds=args.timeout_seconds,
                timeout_policy=args.timeout_policy,
                blockers=args.blockers,
                output_refs=args.output_refs,
            )
            print(format_run_compact(run))
            return 0
        if args.run_command == "steer":
            return _steer_run_worker(args, store)
        if args.run_command == "abort":
            return _abort_run_worker(args, store)
    except RunStoreError as error:
        _print_error(str(error))
        if error.kind == "missing":
            return EX_NOINPUT
        return EX_DATAERR
    return 64


def _start_orchestration_run(args, store):
    if args.prompt is not None:
        return _start_single_worker_run(args, store)
    run = store.load_run(args.name)
    if args.directory is not None:
        run["directory"] = str(Path(args.directory).resolve())
    if args.server is not None:
        run["server_url"] = args.server
    if args.session_id is not None:
        worker = _ensure_orchestration_worker(run, args.worker, role=args.role)
        worker["session_id"] = args.session_id
    if not any(_worker_prompt(worker) for worker in run.get("workers", {}).values() if isinstance(worker, dict)):
        raise RunStoreError(f"run '{args.name}' has no worker prompts; pass --prompt or add workers with --prompt")
    return _start_prompted_workers_run(args, store, run)


def _start_prompted_workers_run(args, store, run):
    try:
        client = OpenCodeApiClient(run["server_url"])
        capabilities = detect_capabilities(client)
        if not capabilities["legacy_fallback_available"]:
            message = (
                "unsupported route behavior: missing legacy POST /session/{sessionID}/run + "
                "POST /session/{sessionID}/reply; v2 prompt admission is not execution"
            )
            _mark_prompted_workers_failed(store, run, message)
            _print_error(message)
            return EX_UNSUPPORTED

        run["status"] = "active"
        _save_orchestration_run(store, run)
        while True:
            workers = run.get("workers", {})
            ready_workers = _ready_prompted_workers(workers)
            if not ready_workers:
                _mark_dependency_blocked_workers(run)
                break
            for worker in ready_workers:
                worker["status"] = "active"
                worker["next_eligible_action"] = "wait"
            _save_orchestration_run(store, run)
            for worker in ready_workers:
                if not worker.get("session_id"):
                    create_response = client.create_session_response(
                        run["directory"], agent=worker.get("agent"), model=worker.get("model")
                    )
                    worker["session_id"] = _session_value(create_response.data, "id", "sessionID", "sessionId")
            _save_orchestration_run(store, run)
            attempt_workers = list(ready_workers)
            while attempt_workers:
                retry_workers = []
                started_workers = []
                for worker in attempt_workers:
                    worker["status"] = "active"
                    worker["next_eligible_action"] = "wait"
                    worker["timeout_started_at"] = _utc_now() if worker.get("timeout_seconds") else None
                    try:
                        run_response = _call_worker_with_timeout(
                            worker,
                            lambda: client.run_session_response(worker["session_id"], _worker_prompt(worker)),
                        )
                    except _WatchTimeout:
                        reason = _worker_timeout_reason(worker)
                        if _schedule_worker_retry(worker, "timeout", reason):
                            worker["timed_out_at"] = _utc_now()
                            retry_workers.append(worker)
                            continue
                        _mark_worker_timeout(worker, reason)
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(reason)
                        return _exit_code_for_orchestration_run(run)
                    except OpenCodeApiError as error:
                        if _schedule_worker_retry(worker, "api", str(error)):
                            retry_workers.append(worker)
                            continue
                        _mark_worker_failed(worker, "api", str(error))
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(f"api failure: {error}")
                        return _exit_code_for_orchestration_run(run)
                    prompt_id = _message_value(run_response.data, "id", "messageID", "messageId")
                    if prompt_id is not None:
                        worker["prompt_ids"] = [prompt_id]
                    provider_error = _provider_failure(run_response.data)
                    if provider_error:
                        if _schedule_worker_retry(worker, "provider", provider_error):
                            retry_workers.append(worker)
                            continue
                        _mark_worker_failed(worker, "provider", provider_error)
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(f"provider failure: {provider_error}")
                        return _exit_code_for_orchestration_run(run)
                    started_workers.append((worker, run_response.data))
                _save_orchestration_run(store, run)
                for worker, run_message in started_workers:
                    try:
                        reply_response = _call_worker_with_timeout(worker, lambda: client.reply_session_response(worker["session_id"]))
                    except _WatchTimeout:
                        reason = _worker_timeout_reason(worker)
                        if _schedule_worker_retry(worker, "timeout", reason):
                            worker["timed_out_at"] = _utc_now()
                            retry_workers.append(worker)
                            continue
                        _mark_worker_timeout(worker, reason)
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(reason)
                        return _exit_code_for_orchestration_run(run)
                    except OpenCodeApiError as error:
                        if _schedule_worker_retry(worker, "api", str(error)):
                            retry_workers.append(worker)
                            continue
                        _mark_worker_failed(worker, "api", str(error))
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(f"api failure: {error}")
                        return _exit_code_for_orchestration_run(run)
                    provider_error = _provider_failure(reply_response.data)
                    if provider_error:
                        if _schedule_worker_retry(worker, "provider", provider_error):
                            retry_workers.append(worker)
                            continue
                        _mark_worker_failed(worker, "provider", provider_error)
                        _mark_dependency_blocked_workers(run)
                        _refresh_orchestration_run_summary(run)
                        _save_orchestration_run(store, run)
                        _print_error(f"provider failure: {provider_error}")
                        return _exit_code_for_orchestration_run(run)
                    result = _run_result(worker["session_id"], run_message, reply_response.data)
                    _apply_worker_result(worker, result)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                if not retry_workers:
                    break
                attempt_workers = retry_workers
            if not _ready_prompted_workers(run.get("workers", {})):
                _mark_dependency_blocked_workers(run)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                if not _pending_prompted_workers(run.get("workers", {})):
                    break
    except OpenCodeApiError as error:
        _mark_prompted_workers_failed(store, run, str(error))
        _print_error(f"api failure: {error}")
        return EX_UNAVAILABLE

    print(format_run_compact(run))
    return _exit_code_for_orchestration_run(run)


class _SmokeFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def _run_smoke(args, client):
    directory = str(Path(args.directory).resolve())
    smoke_id = f"{args.prefix}{uuid.uuid4().hex[:10]}"
    session_id = None
    created_session_ids = []
    result = {
        "status": "active",
        "ok": False,
        "health": None,
        "version": None,
        "directory": directory,
        "prefix": args.prefix,
        "session_id": None,
        "mode": "no-live-model",
        "no_live_model": bool(args.no_live_model),
        "checks": {},
        "event_types": [],
        "cleanup": {"status": "queued", "deleted": [], "verified": []},
    }
    failure = None
    exit_code = EX_UNAVAILABLE

    try:
        capabilities = detect_capabilities(client)
        result["capabilities"] = capabilities
        result["health"] = capabilities["health"]
        result["version"] = capabilities["version"]
        result["checks"]["capabilities"] = {
            "status": "done",
            "health": capabilities["health"],
            "version": capabilities["version"],
        }
        _require_smoke_capabilities(capabilities)

        create_response = client.create_session_response(
            directory,
            title=smoke_id,
            metadata={
                "disposable": True,
                "prefix": args.prefix,
                "smoke_id": smoke_id,
                "no_live_model": bool(args.no_live_model),
            },
        )
        session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
        if not session_id:
            raise _SmokeFailure("session creation response did not include a session id")
        created_session_ids.append(session_id)
        result["session_id"] = session_id
        result["checks"]["create"] = {"status": "done", "session_id": session_id, "title": smoke_id}

        prompt_path = capabilities["route_availability"]["v2_prompt"]["path"]
        event_collector = _SmokeEventCollector(
            client,
            session_id,
            capabilities["route_availability"]["events"]["path"],
            args.event_limit,
        )
        event_collector.start()
        event_collector.wait_open(args.event_timeout)

        steer_message_id = f"msg_{smoke_id}-steer"
        steer_response = client.admit_prompt_response(
            session_id,
            _prompt_admission_payload(steer_message_id, "ocs smoke steer", "steer", prompt_path),
            prompt_path,
        )
        admission = _admission_record(session_id, "steer", steer_message_id, steer_response.data, capabilities=capabilities)
        result["checks"]["steer"] = admission

        event_types = event_collector.collect(args.event_timeout)
        result["event_types"] = event_types
        result["checks"]["events"] = {"status": "done", "types": event_types}

        if args.no_live_model:
            result["checks"]["run_blocking"] = _no_live_run_reply_result(session_id, capabilities)
        else:
            run_response = client.run_session_response(session_id, "ocs smoke")
            provider_error = _provider_failure(run_response.data)
            if provider_error:
                raise _SmokeFailure(f"provider failure: {provider_error}")
            reply_response = client.reply_session_response(session_id)
            provider_error = _provider_failure(reply_response.data)
            if provider_error:
                raise _SmokeFailure(f"provider failure: {provider_error}")
            result["checks"]["run_blocking"] = _run_result(session_id, run_response.data, reply_response.data)

        result["checks"]["blockers"] = _smoke_blocker_summary(client, session_id)
        result["status"] = "done"
        result["ok"] = True
        exit_code = 0
    except _SmokeFailure as error:
        failure = error
        exit_code = error.exit_code
        result["status"] = "failed"
        result["error"] = str(error)
    except OpenCodeApiError as error:
        failure = error
        result["status"] = "failed"
        result["error"] = str(error)

    cleanup = _cleanup_created_sessions(client, created_session_ids)
    result["cleanup"] = cleanup
    result["checks"]["cleanup"] = cleanup
    if cleanup["status"] != "done" and failure is None:
        failure = _SmokeFailure("disposable session cleanup failed")
        result["status"] = "failed"
        result["ok"] = False
        result["error"] = str(failure)
        exit_code = failure.exit_code

    if failure is not None:
        _print_error(f"smoke failed: {failure}; {_format_cleanup_summary(result['cleanup'])}")
        return exit_code

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(_format_smoke_compact(result))
    return 0


def _require_smoke_capabilities(capabilities):
    reasons = unsupported_reasons(capabilities)
    if reasons:
        raise _SmokeFailure(f"unsupported OpenCode server; {'; '.join(reasons)}", exit_code=EX_UNSUPPORTED)
    if not capabilities["v2_prompt_support"]:
        raise _SmokeFailure("unsupported OpenCode server; missing v2 steer admission", exit_code=EX_UNSUPPORTED)
    if not capabilities["event_support"]:
        raise _SmokeFailure(
            "unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event",
            exit_code=EX_UNSUPPORTED,
        )


def _run_live_validate(args, client):
    if os.environ.get(LIVE_VALIDATE_ENV) != "1":
        _print_error(
            f"live-provider validation disabled; set {LIVE_VALIDATE_ENV}=1 to allow token-consuming provider calls"
        )
        return EX_DATAERR

    directory = str(Path(args.directory).resolve())
    validation_id = f"{args.prefix}{uuid.uuid4().hex[:10]}"
    created_session_ids = []
    result = {
        "status": "active",
        "ok": False,
        "mode": "live-provider",
        "gate": {"env": LIVE_VALIDATE_ENV, "enabled": True, "required": "1"},
        "prompt": LIVE_VALIDATE_PROMPT,
        "health": None,
        "version": None,
        "directory": directory,
        "prefix": args.prefix,
        "session_ids": {"steer": None, "run_blocking": None},
        "checks": {},
        "cleanup": {"status": "queued", "deleted": [], "verified": []},
    }
    failure = None
    exit_code = EX_UNAVAILABLE

    def create_live_session(role):
        create_response = client.create_session_response(
            directory,
            agent=args.agent,
            model=args.model,
            title=f"{validation_id}-{role}",
            metadata={
                "disposable": True,
                "kind": "live-provider-validation",
                "live_provider": True,
                "prefix": args.prefix,
                "validation_id": validation_id,
                "role": role,
            },
        )
        session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
        if not session_id:
            raise _LiveValidationFailure("session creation response did not include a session id")
        created_session_ids.append(session_id)
        return session_id

    try:
        capabilities = detect_capabilities(client)
        result["capabilities"] = capabilities
        result["health"] = capabilities["health"]
        result["version"] = capabilities["version"]
        result["checks"]["capabilities"] = {
            "status": "done",
            "health": capabilities["health"],
            "version": capabilities["version"],
        }
        _require_live_validate_capabilities(capabilities)
        result["checks"]["wait"] = _live_wait_record(capabilities)

        steer_session_id = create_live_session("steer")
        result["session_ids"]["steer"] = steer_session_id
        steer_message_id = f"msg_{validation_id}-steer"
        prompt_path = capabilities["route_availability"]["v2_prompt"]["path"]
        steer_response = client.admit_prompt_response(
            steer_session_id,
            _prompt_admission_payload(steer_message_id, LIVE_VALIDATE_PROMPT, "steer", prompt_path),
            prompt_path,
        )
        steer = _admission_record(
            steer_session_id,
            "steer",
            steer_message_id,
            steer_response.data,
            capabilities=capabilities,
        )
        steer.update(_live_steer_execution_observation(client, steer, capabilities))
        result["checks"]["v2_steer"] = steer

        run_session_id = create_live_session("run_blocking")
        result["session_ids"]["run_blocking"] = run_session_id
        run_response = client.run_session_response(run_session_id, LIVE_VALIDATE_PROMPT)
        provider_error = _provider_failure(run_response.data)
        if provider_error:
            raise _LiveValidationFailure(f"provider failure: {provider_error}")
        reply_response = client.reply_session_response(run_session_id)
        provider_error = _provider_failure(reply_response.data)
        if provider_error:
            raise _LiveValidationFailure(f"provider failure: {provider_error}")
        legacy_run_reply = _run_result(run_session_id, run_response.data, reply_response.data)
        legacy_run_reply["succeeded"] = legacy_run_reply["status"] == "done"
        legacy_run_reply["pong"] = _is_exact_pong(legacy_run_reply["text"])
        if not legacy_run_reply["pong"]:
            raise _LiveValidationFailure("live provider did not reply exactly PONG")
        result["checks"]["legacy_run_reply"] = legacy_run_reply
        result["status"] = "done"
        result["ok"] = True
        exit_code = 0
    except _LiveValidationFailure as error:
        failure = error
        exit_code = error.exit_code
        result["status"] = "failed"
        result["error"] = str(error)
    except OpenCodeApiError as error:
        failure = error
        result["status"] = "failed"
        result["error"] = str(error)

    cleanup = _cleanup_created_sessions(client, created_session_ids)
    result["cleanup"] = cleanup
    result["checks"]["cleanup"] = cleanup
    if cleanup["status"] != "done" and failure is None:
        failure = _LiveValidationFailure("disposable live validation session cleanup failed")
        result["status"] = "failed"
        result["ok"] = False
        result["error"] = str(failure)
        exit_code = failure.exit_code

    if failure is not None:
        _print_error(
            f"live-provider validation failed: {failure}; {_format_cleanup_summary(result['cleanup'])}"
        )
        return exit_code

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(_format_live_validate_compact(result))
    return 0


class _LiveValidationFailure(Exception):
    def __init__(self, message, *, exit_code=EX_UNAVAILABLE):
        super().__init__(message)
        self.exit_code = exit_code


def _require_live_validate_capabilities(capabilities):
    reasons = unsupported_reasons(capabilities)
    if not capabilities["v2_prompt_support"]:
        reasons.append("missing v2 steer admission: POST /api/session/{sessionID}/prompt")
    if not capabilities["legacy_fallback_available"]:
        reasons.append(
            "missing legacy run_blocking execution: POST /session/{sessionID}/run + POST /session/{sessionID}/reply"
        )
    if reasons:
        raise _LiveValidationFailure(
            f"unsupported OpenCode server; {'; '.join(reasons)}",
            exit_code=EX_UNSUPPORTED,
        )


def _live_wait_record(capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    return {
        "available": wait_route["available"],
        "api_path": wait_route["path"],
        "status": "available" if wait_route["available"] else "unavailable",
    }


def _live_steer_execution_observation(client, steer, capabilities):
    wait_route = capabilities["route_availability"]["v2_wait"]
    wait_observation = None
    if wait_route["available"] and "?" not in wait_route["path"]:
        try:
            response = client.wait_session_response(steer["session_id"], wait_route["path"])
        except OpenCodeApiError as error:
            wait_observation = _execution_observation(
                "unknown",
                source="wait",
                status="unknown",
                reason="observation_failed",
                error=str(error),
            )
        else:
            status = short_status(_first_present(response.data, "status", "state", "phase"))
            if status in {"active", "done"}:
                return _execution_observation(True, source="wait", status=status, reason="observed_execution_state")
            if status == "queued":
                return _execution_observation(False, source="wait", status=status, reason="observed_not_executed_state")
            wait_observation = _execution_observation("unknown", source="wait", status=status, reason="no_execution_evidence")
    message_observation = _live_message_execution_observation(client, steer)
    if message_observation["executed"] != "unknown":
        return message_observation
    event_route = capabilities["route_availability"]["events"]
    if event_route["available"]:
        return _live_event_execution_observation(client, steer, event_route["path"])
    return message_observation if wait_observation is None else wait_observation


def _live_message_execution_observation(client, steer):
    try:
        session = client.get_session_response(steer["session_id"]).data
    except OpenCodeApiError as error:
        return _execution_observation(
            "unknown",
            source="message",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    status = _assistant_message_status(session)
    if status is not None:
        return _execution_observation(True, source="message", status=status, reason="observed_assistant_message")
    return _execution_observation("unknown", source="message", status="unknown", reason="no_execution_evidence")


def _assistant_message_status(session):
    for message in _iter_message_evidence_candidates(session):
        role = str(_first_present(message, "role", "author", "speaker", "type", "kind") or "").lower()
        if "assistant" not in role:
            continue
        status = short_status(_first_present(message, "status", "state", "phase"))
        if _message_text(message) or _message_tokens(message) is not None or _message_value(message, "cost") is not None:
            return status or "unknown"
        if status in {"active", "done"}:
            return status
    return None


def _live_event_execution_observation(client, steer, event_path):
    try:
        with _watch_deadline(LIVE_EVENT_OBSERVATION_TIMEOUT):
            for raw_event in client.stream_events(event_path):
                event = normalize_event(raw_event, steer["session_id"])
                if event is None:
                    continue
                observation = _event_execution_observation(event)
                if observation["executed"] != "unknown":
                    return observation
                if is_terminal_event(event):
                    break
    except _WatchTimeout:
        return _execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_timed_out",
        )
    except OpenCodeApiError as error:
        return _execution_observation(
            "unknown",
            source="event",
            status="unknown",
            reason="observation_failed",
            error=str(error),
        )
    return _execution_observation("unknown", source="event", status="unknown", reason="no_execution_evidence")


def _event_execution_observation(event):
    status = event.get("status") or "unknown"
    if event.get("kind") in {"text", "tool", "step"}:
        return _execution_observation(True, source="event", status=status, reason="observed_execution_event")
    if event.get("kind") == "status" and status in {"active", "done"}:
        return _execution_observation(True, source="event", status=status, reason="observed_execution_event")
    return _execution_observation("unknown", source="event", status=status, reason="no_execution_evidence")


def _iter_message_evidence_candidates(data):
    if not isinstance(data, dict):
        return
    for key in ("message", "assistant", "reply", "output"):
        value = data.get(key)
        if isinstance(value, dict):
            yield value
    for key in ("messages", "items", "entries"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                yield item


def _execution_observation(executed, *, source, status, reason, error=None):
    evidence = {"source": source, "status": status or "unknown", "reason": reason}
    if error is not None:
        evidence["error"] = error
    return {"executed": executed, "execution_evidence": evidence}


def _is_exact_pong(text):
    return str(text).strip() == "PONG"


def _format_live_validate_compact(result):
    steer = result["checks"].get("v2_steer") or {}
    wait = result["checks"].get("wait") or {}
    legacy_run_reply = result["checks"].get("legacy_run_reply") or {}
    fields = [
        ("status", result["status"]),
        ("mode", result["mode"]),
        ("health", result["health"]),
        ("version", result["version"]),
        ("steer", steer.get("status")),
        ("wait", wait.get("status")),
        ("run", legacy_run_reply.get("status")),
        ("pong", _compact_bool(legacy_run_reply.get("pong"))),
        ("cleanup", result["cleanup"].get("status")),
    ]
    return "live_validate " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


class _SmokeEventCollector:
    def __init__(self, client, session_id, event_path, event_limit):
        self.client = client
        self.session_id = session_id
        self.event_path = event_path
        self.event_limit = event_limit
        self.opened = threading.Event()
        self.items = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def wait_open(self, timeout):
        if self.opened.wait(timeout):
            return
        error = self._first_error()
        if error is not None:
            raise error
        raise _SmokeFailure(f"event stream did not open within {_format_timeout(timeout)}s")

    def collect(self, timeout):
        event_types = []
        deadline = time.monotonic() + timeout
        while len(event_types) < self.event_limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, value = self.items.get(timeout=remaining)
            except queue.Empty:
                break
            if kind == "error":
                raise value
            if kind == "done":
                break
            event = value
            event_type = event.get("type") or event.get("kind")
            if event_type and event_type not in event_types:
                event_types.append(event_type)
            if is_terminal_event(event):
                break
        if event_types:
            return event_types
        if self.thread.is_alive():
            raise _SmokeFailure(f"event stream timed out after {_format_timeout(timeout)}s")
        raise _SmokeFailure("event stream produced no events for disposable session")

    def _run(self):
        try:
            for raw_event in self.client.stream_events(self.event_path, on_open=self.opened.set):
                event = normalize_event(raw_event, self.session_id)
                if event is None:
                    continue
                self.items.put(("event", event))
        except OpenCodeApiError as error:
            self.items.put(("error", error))
        finally:
            self.items.put(("done", None))

    def _first_error(self):
        pending = []
        error = None
        while True:
            try:
                item = self.items.get_nowait()
            except queue.Empty:
                break
            pending.append(item)
            if item[0] == "error":
                error = item[1]
                break
        for item in pending:
            self.items.put(item)
        return error


def _smoke_blocker_summary(client, session_id):
    try:
        permissions = _filter_blockers_by_session(_collection_blockers(client.list_permissions_response().data, "permissions"), session_id)
        questions = _filter_blockers_by_session(_collection_blockers(client.list_questions_response().data, "questions"), session_id)
    except OpenCodeApiError as error:
        return {"status": "skipped", "error": str(error), "permissions": None, "questions": None, "total": None}
    return {"status": "done", "permissions": len(permissions), "questions": len(questions), "total": len(permissions) + len(questions)}


def _cleanup_created_sessions(client, session_ids):
    cleanup = {"status": "done", "deleted": [], "verified": [], "errors": []}
    if not session_ids:
        return cleanup
    for session_id in session_ids:
        error = _delete_and_verify_session(client, session_id)
        if error is not None:
            cleanup["errors"].append({"session_id": session_id, "error": str(error)})
            cleanup["status"] = "failed"
            continue
        cleanup["deleted"].append(session_id)
        cleanup["verified"].append(session_id)
    return cleanup


def _delete_and_verify_session(client, session_id):
    try:
        client.delete_session_response(session_id)
    except OpenCodeApiError as error:
        if error.status != 404:
            return error
    try:
        client.get_session(session_id)
    except OpenCodeApiError as error:
        if error.status == 404:
            return None
        return error
    return OpenCodeApiError(f"delete verification failed; session {session_id} is still readable")


def _format_smoke_compact(result):
    run = result["checks"].get("run_blocking") or {}
    blockers = result["checks"].get("blockers") or {}
    fields = [
        ("status", result["status"]),
        ("health", result["health"]),
        ("version", result["version"]),
        ("session", result["session_id"]),
        ("steer", (result["checks"].get("steer") or {}).get("status")),
        ("run", run.get("status")),
        ("events", _compact_list(result.get("event_types"))),
        ("blockers", blockers.get("total")),
        ("cleanup", result["cleanup"].get("status")),
        ("no_live_model", _compact_bool(result["no_live_model"])),
    ]
    return "smoke " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_cleanup_summary(cleanup):
    return " ".join(
        [
            f"cleanup={cleanup.get('status')}",
            f"deleted={len(cleanup.get('deleted') or [])}",
            f"verified={len(cleanup.get('verified') or [])}",
        ]
    )


def _cleanup_disposable_command(args, client):
    directory = str(Path(args.directory).resolve()) if args.directory else None
    try:
        response = client.list_sessions_response()
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE

    sessions = [
        session
        for session in _collection_sessions(response.data)
        if _is_disposable_session(session, prefix=args.prefix, directory=directory)
    ]
    result = {
        "status": "done",
        "prefix": args.prefix,
        "directory": directory,
        "stale": len(sessions),
        "sessions": [_session_value(session, "id", "sessionID", "sessionId") for session in sessions],
        "deleted": [],
        "verified": [],
        "errors": [],
    }
    for session in sessions:
        session_id = _session_value(session, "id", "sessionID", "sessionId")
        if not session_id:
            result["status"] = "failed"
            result["errors"].append({"session_id": None, "error": "session has no id"})
            continue
        error = _delete_and_verify_session(client, session_id)
        if error is not None:
            result["status"] = "failed"
            result["errors"].append({"session_id": session_id, "error": str(error)})
            continue
        result["deleted"].append(session_id)
        result["verified"].append(session_id)

    if result["status"] != "done":
        _print_error(f"cleanup failed: {_format_cleanup_command_compact(result)}")
        return EX_UNAVAILABLE
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(_format_cleanup_command_compact(result))
    return 0


def _is_disposable_session(session, *, prefix, directory):
    if directory is not None and _session_value(session, "directory", "cwd") != directory:
        return False
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    values = [
        _session_value(session, "id", "sessionID", "sessionId"),
        _session_value(session, "title", "name"),
        metadata.get("smoke_id"),
        metadata.get("prefix"),
        metadata.get("disposable_prefix"),
    ]
    return any(str(value).startswith(prefix) for value in values if value is not None)


def _format_cleanup_command_compact(result):
    fields = [
        ("stale", result["stale"]),
        ("deleted", len(result["deleted"])),
        ("verified", len(result["verified"])),
        ("prefix", result["prefix"]),
        ("dir", result["directory"]),
    ]
    return "cleanup " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _start_single_worker_run(args, store):
    run = _load_or_create_orchestration_run(store, args)
    worker = _ensure_orchestration_worker(run, args.worker, role=args.role)
    run["status"] = "active"
    worker["status"] = "active"
    _save_orchestration_run(store, run)

    created_session_id = None
    try:
        client = OpenCodeApiClient(run["server_url"])
        capabilities = detect_capabilities(client)
        if not capabilities["legacy_fallback_available"]:
            message = (
                "unsupported route behavior: missing legacy POST /session/{sessionID}/run + "
                "POST /session/{sessionID}/reply; v2 prompt admission is not execution"
            )
            _mark_orchestration_failed(store, run, worker, message)
            _print_error(message)
            return EX_UNSUPPORTED

        session_id = args.session_id or worker.get("session_id")
        if session_id is None:
            create_response = client.create_session_response(run["directory"], agent=args.agent, model=args.model)
            session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
            created_session_id = session_id
        worker["session_id"] = session_id
        _save_orchestration_run(store, run)

        if args.agent is not None:
            worker["agent"] = args.agent
        if args.model is not None:
            worker["model"] = args.model

        while True:
            worker["status"] = "active"
            worker["next_eligible_action"] = "wait"
            worker["timeout_started_at"] = _utc_now() if worker.get("timeout_seconds") else None
            try:
                run_response = _call_worker_with_timeout(worker, lambda: client.run_session_response(session_id, args.prompt))
            except _WatchTimeout:
                reason = _worker_timeout_reason(worker)
                if _schedule_worker_retry(worker, "timeout", reason):
                    worker["timed_out_at"] = _utc_now()
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_timeout(worker, reason)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(reason)
                return _exit_code_for_orchestration_run(run)
            except OpenCodeApiError as error:
                if _schedule_worker_retry(worker, "api", str(error)):
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_failed(worker, "api", str(error))
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(f"api failure: {error}")
                return _exit_code_for_orchestration_run(run)

            prompt_id = _message_value(run_response.data, "id", "messageID", "messageId")
            if prompt_id is not None:
                worker["prompt_ids"] = [prompt_id]
                _save_orchestration_run(store, run)

            provider_error = _provider_failure(run_response.data)
            if provider_error:
                if _schedule_worker_retry(worker, "provider", provider_error):
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_failed(worker, "provider", provider_error)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(f"provider failure: {provider_error}")
                return _exit_code_for_orchestration_run(run)

            event_route = capabilities["route_availability"]["events"]
            if event_route["available"]:
                _stream_orchestration_progress(client, session_id, event_route["path"])

            try:
                reply_response = _call_worker_with_timeout(worker, lambda: client.reply_session_response(session_id))
            except _WatchTimeout:
                reason = _worker_timeout_reason(worker)
                if _schedule_worker_retry(worker, "timeout", reason):
                    worker["timed_out_at"] = _utc_now()
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_timeout(worker, reason)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(reason)
                return _exit_code_for_orchestration_run(run)
            except OpenCodeApiError as error:
                if _schedule_worker_retry(worker, "api", str(error)):
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_failed(worker, "api", str(error))
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(f"api failure: {error}")
                return _exit_code_for_orchestration_run(run)
            provider_error = _provider_failure(reply_response.data)
            if provider_error:
                if _schedule_worker_retry(worker, "provider", provider_error):
                    _save_orchestration_run(store, run)
                    continue
                _mark_worker_failed(worker, "provider", provider_error)
                _refresh_orchestration_run_summary(run)
                _save_orchestration_run(store, run)
                _print_error(f"provider failure: {provider_error}")
                return _exit_code_for_orchestration_run(run)
            break
    except OpenCodeApiError as error:
        _mark_orchestration_failed(store, run, worker, str(error))
        _print_error(f"api failure: {error}")
        return EX_UNAVAILABLE

    result = _run_result(session_id, run_response.data, reply_response.data)
    _apply_worker_result(worker, result)
    _refresh_orchestration_run_summary(run)
    if args.cleanup:
        worker["cleanup"] = {"requested": True, "deleted": False}
        if created_session_id is not None:
            try:
                client.delete_session(created_session_id)
            except OpenCodeApiError as error:
                worker["cleanup"]["error"] = str(error)
                _mark_orchestration_failed(store, run, worker, str(error))
                _print_cleanup_error(error)
                return EX_UNAVAILABLE
            worker["cleanup"]["deleted"] = True
    _save_orchestration_run(store, run)
    print(format_run_compact(run))
    return _exit_code_for_orchestration_run(run)


def _collect_run_results(args, store):
    run = store.load_run(args.name)
    workers = run.get("workers", {})
    if args.worker is not None:
        return _collect_single_worker_result(args, run, args.worker)
    if len(workers) == 1:
        worker_id = next(iter(workers))
        return _collect_single_worker_result(args, run, worker_id)
    completed_workers = [
        worker for worker in _workers_in_dependency_order(workers) if isinstance(worker.get("result"), dict)
    ]
    if not completed_workers:
        raise RunStoreError(f"run '{args.name}' has no collected worker results", kind="missing")
    if args.json:
        print(
            json.dumps(
                [
                    {"worker": worker.get("id"), "role": worker.get("role"), "result": worker.get("result")}
                    for worker in completed_workers
                ],
                sort_keys=True,
            )
        )
        return 0
    print("\n".join(_format_worker_result_compact(worker) for worker in completed_workers))
    return 0


def _collect_single_worker_result(args, run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not isinstance(worker, dict):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{args.name}'", kind="missing")
    result = worker.get("result")
    if not isinstance(result, dict):
        raise RunStoreError(f"worker '{worker_id}' in run '{args.name}' has no collected result", kind="missing")
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    print(_format_run_compact(result))
    return 0


def _steer_run_worker(args, store):
    run = store.load_run(args.name)
    worker = _run_worker_with_session(run, args.worker_id)
    try:
        client = OpenCodeApiClient(run["server_url"])
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE
    if not capabilities["v2_prompt_support"]:
        _print_error(
            "unsupported v2 prompt capability; durable prompt admission requires "
            "POST /api/session/{sessionID}/prompt or POST /session/{sessionID}/prompt_async; "
            "legacy run/reply fallback is not used for steer admission"
        )
        return EX_UNSUPPORTED

    message_id = args.message_id or f"msg_{uuid.uuid4().hex}"
    prompt_path = capabilities["route_availability"]["v2_prompt"]["path"]
    payload = _prompt_admission_payload(message_id, args.text, args.delivery, prompt_path)
    try:
        response = client.admit_prompt_response(worker["session_id"], payload, prompt_path)
    except OpenCodeApiError as error:
        if error.status in {400, 404, 405, 415, 422}:
            _print_error(f"unsupported v2 prompt behavior; {_api_error_detail(error)}; legacy run/reply fallback is not used")
            return EX_UNSUPPORTED
        _print_error(f"prompt admission failed; {error}; legacy run/reply fallback is not used")
        return EX_UNAVAILABLE

    admission = _admission_record(worker["session_id"], args.delivery, message_id, response.data, capabilities=capabilities)
    prompt_ids = worker.setdefault("prompt_ids", [])
    if admission["message_id"] not in prompt_ids:
        prompt_ids.append(admission["message_id"])
    _save_orchestration_run(store, run)
    if args.json:
        print(json.dumps({"run": run["name"], "worker": worker["id"], "admission": admission}, sort_keys=True))
    else:
        print(f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {_format_admission_compact(admission)}")
    return 0


def _abort_run_worker(args, store):
    run = store.load_run(args.name)
    worker = _run_worker_with_session(run, args.worker_id)
    try:
        client = OpenCodeApiClient(run["server_url"])
        response = client.abort_session_response(worker["session_id"])
    except OpenCodeApiError as error:
        if _is_session_not_found_error(error):
            _print_error(f"session not found: {worker['session_id']}")
        else:
            _print_error(str(error))
        return EX_UNAVAILABLE
    abort = _abort_record(worker["session_id"], response.data)
    if abort["accepted"]:
        worker["status"] = "aborted"
    worker["abort"] = abort
    _refresh_orchestration_run_summary(run)
    _save_orchestration_run(store, run)
    if args.json:
        print(json.dumps({"run": run["name"], "worker": worker["id"], "abort": abort}, sort_keys=True))
    else:
        print(f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {_format_abort_compact(abort)}")
    return 0


def _run_worker_with_session(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not isinstance(worker, dict):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    if not worker.get("session_id"):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no session", kind="missing")
    return worker


def _load_or_create_orchestration_run(store, args):
    try:
        run = store.load_run(args.name)
    except RunStoreError as error:
        if error.kind != "missing":
            raise
        run = store.create_run(
            args.name,
            directory=args.directory or ".",
            server_url=args.server or _server_default(),
        )
    else:
        if args.directory is not None:
            run["directory"] = str(Path(args.directory).resolve())
        if args.server is not None:
            run["server_url"] = args.server
    return run


def _ensure_orchestration_worker(run, worker_id, *, role):
    workers = run.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not isinstance(worker, dict):
        worker = {}
    worker.setdefault("id", worker_id)
    worker.setdefault("role", role)
    worker.setdefault("session_id", None)
    worker.setdefault("agent", None)
    worker.setdefault("model", None)
    worker.setdefault("dependencies", [])
    worker.setdefault("prompt_ids", [])
    worker.setdefault("retry_count", 0)
    worker.setdefault("retry_limit", 0)
    worker.setdefault("retryable_failures", [])
    worker.setdefault("timeout_seconds", None)
    worker.setdefault("timeout_policy", "timeout")
    worker.setdefault("timeout_started_at", None)
    worker.setdefault("timed_out_at", None)
    worker.setdefault("failure_category", None)
    worker.setdefault("failure_reason", None)
    worker.setdefault("last_failure_category", None)
    worker.setdefault("last_failure_reason", None)
    worker.setdefault("next_eligible_action", "start")
    worker.setdefault("blockers", [])
    worker.setdefault("output_refs", [])
    if not worker.get("role"):
        worker["role"] = role
    worker["id"] = worker_id
    workers[worker_id] = worker
    return worker


def _stream_orchestration_progress(client, session_id, event_path):
    for raw_event in client.stream_events(event_path):
        event = normalize_event(raw_event, session_id)
        if event is None:
            continue
        print(format_watch_event(event), flush=True)
        if is_terminal_event(event):
            return


def _mark_orchestration_failed(store, run, worker, error):
    run["status"] = "failed"
    worker["status"] = "failed"
    worker["error"] = error
    worker["failure_category"] = "api"
    worker["failure_reason"] = error
    worker["last_failure_category"] = "api"
    worker["last_failure_reason"] = error
    worker["next_eligible_action"] = "none"
    _save_orchestration_run(store, run)


def _mark_worker_failed(worker, category, reason):
    worker["status"] = "failed"
    worker["error"] = reason
    worker["failure_category"] = category
    worker["failure_reason"] = reason
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry" if _worker_retry_available(worker, category) else "none"


def _schedule_worker_retry(worker, category, reason):
    if not _worker_retry_available(worker, category):
        return False
    worker["retry_count"] = int(worker.get("retry_count") or 0) + 1
    worker["status"] = "active"
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["last_failure_category"] = category
    worker["last_failure_reason"] = reason
    worker["next_eligible_action"] = "retry"
    return True


def _worker_retry_available(worker, category):
    retryable = set(worker.get("retryable_failures") or [])
    if category not in retryable and "all" not in retryable:
        return False
    try:
        retry_count = int(worker.get("retry_count") or 0)
        retry_limit = int(worker.get("retry_limit") or 0)
    except (TypeError, ValueError):
        return False
    return retry_count < retry_limit


def _call_worker_with_timeout(worker, callback):
    timeout = worker.get("timeout_seconds")
    if timeout is None:
        return callback()
    with _watch_deadline(timeout):
        return callback()


def _worker_timeout_reason(worker):
    return f"worker timed out after {_format_timeout(worker.get('timeout_seconds'))}s"


def _mark_worker_timeout(worker, reason):
    status = worker.get("timeout_policy") or "timeout"
    if status == "failed":
        status = "failed"
    worker["status"] = status
    worker["error"] = reason
    worker["failure_category"] = "timeout"
    worker["failure_reason"] = reason
    worker["last_failure_category"] = "timeout"
    worker["last_failure_reason"] = reason
    worker["timed_out_at"] = _utc_now()
    worker["output_refs"] = []
    if status == "blocked":
        worker["blockers"] = ["timeout"]
        worker["next_eligible_action"] = "resolve_blocker"
    else:
        worker["next_eligible_action"] = "none"


def _apply_worker_result(worker, result):
    worker["result"] = result
    worker["status"] = result["status"]
    worker["failure_category"] = None
    worker["failure_reason"] = None
    worker["next_eligible_action"] = "collect" if result["status"] == "done" else "none"
    assistant_message_id = result["message_ids"].get("assistant")
    worker["output_refs"] = [f"assistant:{assistant_message_id}"] if result["status"] == "done" and assistant_message_id else []


def _exit_code_for_orchestration_run(run):
    status = run.get("status")
    if status == "done":
        return 0
    if status == "timeout":
        return EX_TIMEOUT
    if status == "blocked":
        return EX_BLOCKED
    if status == "aborted":
        return EX_ABORTED
    if _has_partial_worker_success(run):
        return EX_PARTIAL
    return EX_UNAVAILABLE


def _has_partial_worker_success(run):
    workers = [worker for worker in (run.get("workers") or {}).values() if isinstance(worker, dict) and _worker_prompt(worker)]
    if not workers:
        return False
    statuses = {worker.get("status") for worker in workers}
    return "done" in statuses and any(status in {"failed", "blocked", "aborted", "timeout"} for status in statuses)


def _mark_prompted_workers_failed(store, run, error):
    run["status"] = "failed"
    for worker in run.get("workers", {}).values():
        if isinstance(worker, dict) and _worker_prompt(worker) and worker.get("status") not in {"done", "failed"}:
            _mark_worker_failed(worker, "api", error)
    _save_orchestration_run(store, run)


def _ready_prompted_workers(workers):
    ready = []
    for worker_id in sorted(workers):
        worker = workers[worker_id]
        if not isinstance(worker, dict) or not _worker_prompt(worker):
            continue
        if worker.get("status") in {"done", "failed", "aborted", "timeout"}:
            continue
        if _dependencies_done(worker, workers):
            ready.append(worker)
    return ready


def _pending_prompted_workers(workers):
    return [
        worker
        for worker in workers.values()
        if isinstance(worker, dict)
        and _worker_prompt(worker)
        and worker.get("status") not in {"done", "failed", "aborted", "timeout", "blocked"}
    ]


def _mark_dependency_blocked_workers(run):
    workers = run.get("workers", {})
    for worker in workers.values():
        if not isinstance(worker, dict) or not _worker_prompt(worker):
            continue
        if worker.get("status") in {"done", "failed", "aborted", "timeout"}:
            continue
        if _dependencies_failed(worker, workers):
            worker["status"] = "blocked"
            worker["blockers"] = [f"dependency:{dependency}" for dependency in worker.get("dependencies", [])]


def _dependencies_done(worker, workers):
    for dependency in worker.get("dependencies", []):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict) or dependency_worker.get("status") != "done":
            return False
    return True


def _dependencies_failed(worker, workers):
    for dependency in worker.get("dependencies", []):
        dependency_worker = workers.get(dependency)
        if not isinstance(dependency_worker, dict):
            return True
        if dependency_worker.get("status") in {"failed", "aborted", "timeout", "blocked"}:
            return True
    return False


def _refresh_orchestration_run_summary(run):
    workers = run.get("workers", {})
    prompted_workers = [worker for worker in workers.values() if isinstance(worker, dict) and _worker_prompt(worker)]
    status_workers = prompted_workers or [worker for worker in workers.values() if isinstance(worker, dict)]
    run["output_refs"] = _worker_output_refs_in_dependency_order(workers)
    if not status_workers:
        return
    statuses = {worker.get("status") for worker in status_workers}
    if statuses == {"done"}:
        run["status"] = "done"
    elif any(status == "failed" for status in statuses):
        run["status"] = "failed"
    elif any(status == "aborted" for status in statuses):
        run["status"] = "aborted"
    elif any(status == "timeout" for status in statuses):
        run["status"] = "timeout"
    elif any(status == "blocked" for status in statuses):
        run["status"] = "blocked"
    elif any(status == "active" for status in statuses):
        run["status"] = "active"
    else:
        run["status"] = "queued"


def _worker_output_refs_in_dependency_order(workers):
    ordered = []
    for worker in _workers_in_dependency_order(workers):
        worker_id = worker.get("id")
        if worker.get("status") != "done":
            continue
        for output_ref in worker.get("output_refs", []):
            if isinstance(output_ref, str) and output_ref.startswith("assistant:"):
                ordered.append(f"{worker_id}:{output_ref.split(':', 1)[1]}")
            else:
                ordered.append(f"{worker_id}:{output_ref}")
    return ordered


def _workers_in_dependency_order(workers):
    ordered = []
    visited = set()
    visiting = set()

    def visit(worker_id):
        if worker_id in visited or worker_id in visiting:
            return
        visiting.add(worker_id)
        worker = workers.get(worker_id)
        if isinstance(worker, dict):
            for dependency in worker.get("dependencies", []):
                visit(dependency)
            ordered.append(worker)
        visiting.remove(worker_id)
        visited.add(worker_id)

    for worker_id in sorted(workers):
        visit(worker_id)
    return ordered


def _format_worker_result_compact(worker):
    result = worker["result"]
    fields = [
        ("worker", worker.get("id")),
        ("role", worker.get("role")),
        ("session", result["session_id"]),
        ("status", result["status"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("cost", result["cost"]),
        ("tokens", _tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _worker_prompt(worker):
    prompt = worker.get("prompt")
    if prompt is None:
        return None
    return str(prompt)


def _save_orchestration_run(store, run):
    run["updated_at"] = _utc_now()
    store.save_run(run)


def _server_default():
    return os.environ.get("OPENCODE_SERVER_URL") or os.environ.get("OPENCODE_SERVER") or DEFAULT_SERVER_URL


def _print_error(message):
    print(f"{CLI_NAME}: {message}", file=sys.stderr)


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    output.add_argument("--json", action="store_true", help="print JSON data")
    output.add_argument("--raw", action="store_true", help="print raw API response body")


def _add_admission_arguments(parser):
    parser.add_argument("session_id", help="session ID to admit input to")
    parser.add_argument("text", help="input text to admit")
    parser.add_argument(
        "--delivery",
        choices=("steer", "queue"),
        default="steer",
        help="admission delivery mode; queue admits input without competing as a top-level command",
    )
    parser.add_argument("--message-id", help="client-supplied prompt/message ID for idempotent admission")
    _add_server_argument(parser)
    _add_output_arguments(parser)


def _admit_prompt(args, client, delivery):
    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE

    if not capabilities["v2_prompt_support"]:
        print(
            f"{CLI_NAME}: unsupported v2 prompt capability; durable prompt admission requires "
            "POST /api/session/{sessionID}/prompt or POST /session/{sessionID}/prompt_async; "
            "legacy run/reply fallback is not used for steer admission",
            file=sys.stderr,
        )
        return EX_UNSUPPORTED

    message_id = args.message_id or f"msg_{uuid.uuid4().hex}"
    prompt_path = capabilities["route_availability"]["v2_prompt"]["path"]
    payload = _prompt_admission_payload(message_id, args.text, delivery, prompt_path)

    try:
        response = client.admit_prompt_response(
            args.session_id,
            payload,
            prompt_path,
        )
    except OpenCodeApiError as error:
        if _is_idempotent_admission_replay(error, message_id):
            if args.raw:
                _write_raw(error.body or "")
                return 0
            admission = _admission_record(
                args.session_id,
                delivery,
                message_id,
                error.data,
                capabilities=capabilities,
            )
            if args.json:
                print(json.dumps(admission, sort_keys=True))
            else:
                print(_format_admission_compact(admission))
            return 0
        if error.status in {400, 404, 405, 415, 422}:
            print(
                f"{CLI_NAME}: unsupported v2 prompt behavior; {_api_error_detail(error)}; "
                "legacy run/reply fallback is not used",
                file=sys.stderr,
            )
            return EX_UNSUPPORTED
        print(
            f"{CLI_NAME}: prompt admission failed; {error}; legacy run/reply fallback is not used",
            file=sys.stderr,
        )
        return EX_UNAVAILABLE

    if args.raw:
        _write_raw(response.body)
        return 0

    admission = _admission_record(args.session_id, delivery, message_id, response.data, capabilities=capabilities)
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
                _print_error(str(error))
                return EX_UNAVAILABLE

            event_route = capabilities["route_availability"]["events"]
            if not event_route["available"]:
                print(
                    f"{CLI_NAME}: unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event",
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
                _print_error(f"event stream failure: {error}")
                if _is_invalid_event_stream(error):
                    return EX_DATAERR
                return EX_UNAVAILABLE
            flush_pending_text()
            return 0
    except _WatchTimeout:
        flush_pending_text()
        _print_error(f"watch timed out after {_format_timeout(args.timeout)}s")
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


def _positive_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
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


class _BlockingProviderFailure(Exception):
    pass


def _blocking_execution_capabilities(doc):
    blocking_message_available = blocking_message_supported(doc)
    legacy_fallback_available = legacy_run_reply_supported(doc)
    return {
        "route_availability": {
            "blocking_message": _execution_route(SESSION_MESSAGE_PATH, "POST", blocking_message_available),
            "legacy_run": _execution_route(LEGACY_RUN_PATH, "POST", legacy_fallback_available),
            "legacy_reply": _execution_route(LEGACY_REPLY_PATH, "POST", legacy_fallback_available),
        },
        "blocking_message_available": blocking_message_available,
        "blocking_execution_available": blocking_message_available or legacy_fallback_available,
        "legacy_fallback_available": legacy_fallback_available,
    }


def _execution_route(path, method, available):
    return {"path": path, "method": method, "available": available}


def _blocking_execution_strategy(capabilities):
    routes = capabilities.get("route_availability") or {}
    blocking_message = routes.get("blocking_message") or {}
    if capabilities.get("blocking_message_available") or blocking_message.get("available"):
        return "session_message"
    if capabilities.get("legacy_fallback_available"):
        return "legacy_run_reply"
    return None


def _unsupported_blocking_execution_message():
    return (
        "unsupported route behavior: missing blocking execution: POST /session/{sessionID}/message or legacy "
        "POST /session/{sessionID}/run + POST /session/{sessionID}/reply; v2 prompt admission is not execution"
    )


def _execute_blocking_prompt(client, session_id, prompt, capabilities):
    strategy = _blocking_execution_strategy(capabilities)
    if strategy == "session_message":
        return _execute_session_message_prompt(client, session_id, prompt, capabilities)
    if strategy == "legacy_run_reply":
        return _execute_legacy_run_reply_prompt(client, session_id, prompt)
    raise OpenCodeApiError(_unsupported_blocking_execution_message())


def _execute_session_message_prompt(client, session_id, prompt, capabilities):
    message_id = f"msg_{uuid.uuid4().hex}"
    response = _call_with_client_timeout(
        client,
        BLOCKING_EXECUTION_TIMEOUT_SECONDS,
        lambda: client.message_session_response(session_id, prompt, message_id=message_id),
    )
    provider_error = _provider_failure(response.data)
    if provider_error:
        raise _BlockingProviderFailure(provider_error)
    return _session_message_result(session_id, message_id, response.data, capabilities)


def _execute_legacy_run_reply_prompt(client, session_id, prompt):
    run_response = _call_with_client_timeout(
        client,
        BLOCKING_EXECUTION_TIMEOUT_SECONDS,
        lambda: client.run_session_response(session_id, prompt),
    )
    provider_error = _provider_failure(run_response.data)
    if provider_error:
        raise _BlockingProviderFailure(provider_error)
    reply_response = _call_with_client_timeout(
        client,
        BLOCKING_EXECUTION_TIMEOUT_SECONDS,
        lambda: client.reply_session_response(session_id),
    )
    provider_error = _provider_failure(reply_response.data)
    if provider_error:
        raise _BlockingProviderFailure(provider_error)
    return _run_result(session_id, run_response.data, reply_response.data)


def _call_with_client_timeout(client, timeout, callback):
    previous_timeout = client.timeout
    client.timeout = max(previous_timeout or 0, timeout)
    try:
        return callback()
    finally:
        client.timeout = previous_timeout


def _session_message_result(session_id, prompt_message_id, assistant_message, capabilities):
    raw_status = _message_value(assistant_message, "status") or "completed"
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": prompt_message_id,
            "assistant": _message_value(assistant_message, "id", "messageID", "messageId"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": {"message": SESSION_MESSAGE_PATH},
        "execution_strategy": "session_message",
        "fallback": {
            "available": capabilities.get("legacy_fallback_available", False),
            "strategy": "legacy_run_reply",
            "used": False,
        },
        "cost": _message_value(assistant_message, "cost"),
        "tokens": _message_tokens(assistant_message),
        "text": _message_text(assistant_message),
    }


def _run_result(session_id, run_message, reply_message):
    raw_status = _message_value(reply_message, "status") or "completed"
    status = short_status(raw_status)
    return {
        "session_id": session_id,
        "message_ids": {
            "user": _message_value(run_message, "id", "messageID", "messageId"),
            "assistant": _message_value(reply_message, "id", "messageID", "messageId"),
        },
        "status": status,
        "raw_status": raw_status,
        "terminal_state": status,
        "api_path": {"run": LEGACY_RUN_PATH, "reply": LEGACY_REPLY_PATH},
        "execution_strategy": "legacy_run_reply",
        "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
        "cost": _message_value(reply_message, "cost"),
        "tokens": _message_tokens(reply_message),
        "text": _message_text(reply_message),
    }


def _no_live_run_reply_result(session_id, capabilities):
    routes = capabilities["route_availability"]
    return {
        "session_id": session_id,
        "status": "skipped",
        "reason": "no-live-model",
        "raw_status": "skipped",
        "terminal_state": "skipped",
        "api_path": {"run": routes["legacy_run"]["path"], "reply": routes["legacy_reply"]["path"]},
        "fallback": {
            "available": capabilities["legacy_fallback_available"],
            "strategy": "legacy_run_reply",
            "used": False,
        },
    }


def _format_run_compact(result):
    fields = [
        ("session", result["session_id"]),
        ("status", result["status"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("cost", result["cost"]),
        ("tokens", _tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return "run_blocking " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


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
    _print_error(f"api failure: disposable session cleanup failed: {error}")


def _is_session_not_found_error(error):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    parts = path.split("/")
    if method == "POST" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] in {"run", "reply", "message", "abort", "fork"}
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
    error = _message_value(message, "error", "reason", "message")
    if status not in {"failed", "error", "errored"}:
        if not status and error:
            if isinstance(error, dict):
                return error.get("message") or json.dumps(error, sort_keys=True)
            return str(error)
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, sort_keys=True)
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


def _format_session_table(sessions, blocker_counts=None):
    headers = ["id", "title", "dir", "agent", "model", "cost", "tokens", "updated"]
    if blocker_counts is not None:
        headers.extend(["permissions", "questions", "blockers"])
    rows = []
    for session in sessions:
        row = [
            _session_value(session, "id", "sessionID", "sessionId"),
            _session_value(session, "title"),
            _session_value(session, "directory", "cwd"),
            _session_value(session, "agent"),
            _session_value(session, "model"),
            _session_value(session, "cost"),
            _session_tokens(session),
            _session_value(session, "updatedAt", "updated_at"),
        ]
        if blocker_counts is not None:
            counts = _counts_for_session(blocker_counts, session)
            row.extend([counts["permissions"], counts["questions"], counts["total"]])
        rows.append(row)
    return _format_table(headers, rows)


def _format_table(headers, rows):
    lines = ["\t".join(headers)]
    lines.extend("\t".join(_compact_value(value) for value in row) for row in rows)
    return "\n".join(lines)


def _format_admission_compact(admission):
    fields = [
        ("session", admission["session_id"]),
        ("message", admission["message_id"]),
        ("delivery", admission["delivery"]),
        ("status", admission["status"]),
        ("admitted", admission["admitted_sequence"]),
        ("promoted", admission["promoted_sequence"]),
    ]
    return "steer " + " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


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
    raw_status = _first_present(data, "status", "state")
    accepted = _bool_value(_first_present(data, "accepted", "aborted", "ok", "success"))
    if accepted is None and str(raw_status or "").lower() in {"accepted", "aborting", "abort", "aborted", "cancelled", "canceled"}:
        accepted = True
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id", "id") or session_id,
        "accepted": accepted if accepted is not None else True,
        "status": short_status(raw_status),
        "raw_status": raw_status,
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


def _admission_record(session_id, delivery, message_id, data, *, capabilities):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        data = {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    state = _first_present(data, "state", "status", "phase") or "admitted"
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id")
        or _first_present(info, "sessionID", "sessionId", "session_id")
        or session_id,
        "message_id": _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
        or message_id,
        "delivery": _first_present(data, "delivery", "deliveryMode", "mode") or delivery,
        "state": state,
        "raw_state": state,
        "status": short_status(state),
        "terminal_state": None,
        "api_path": capabilities["route_availability"]["v2_prompt"]["path"],
        "fallback": {
            "available": capabilities["legacy_fallback_available"],
            "strategy": "legacy_run_reply",
            "used": False,
        },
        "admitted_sequence": _first_present(data, "admittedSeq", "admittedSequence", "admitted_sequence", "sequence"),
        "promoted_sequence": _first_present(data, "promotedSeq", "promotedSequence", "promoted_sequence"),
    }


def _prompt_admission_payload(message_id, text, delivery, prompt_path):
    if prompt_path.split("?", 1)[0].rstrip("/") == "/api/session/{sessionID}/prompt":
        return {"id": message_id, "prompt": {"text": text}, "delivery": delivery}
    return {
        "messageID": message_id,
        "parts": [{"type": "text", "text": text}],
        "delivery": delivery,
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
    session = _session_record(session)
    for name in names:
        value = session.get(name)
        if value is not None:
            return value
    location = session.get("location")
    if isinstance(location, dict):
        for name in names:
            if name in {"directory", "cwd"} and location.get("directory") is not None:
                return location.get("directory")
    time = session.get("time")
    if isinstance(time, dict):
        for name in names:
            if name in {"createdAt", "created_at"} and time.get("created") is not None:
                return time.get("created")
            if name in {"updatedAt", "updated_at"} and time.get("updated") is not None:
                return time.get("updated")
    return None


def _session_record(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        return session["data"]
    return session if isinstance(session, dict) else {}


def _first_present(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _session_tokens(session):
    session = _session_record(session)
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
