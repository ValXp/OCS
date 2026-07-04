import argparse
import json
import os
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import (
    BlockingProviderFailure as _BlockingProviderFailure,
    blocking_execution_capabilities as _blocking_execution_capabilities,
    blocking_execution_strategy as _blocking_execution_strategy,
    execute_blocking_prompt as _execute_blocking_prompt,
    format_blocking_execution_compact as _format_run_compact,
    tokens_total as _tokens_total,
    unsupported_blocking_execution_message as _unsupported_blocking_execution_message,
)
from opencode_session.capabilities import detect_capabilities
from opencode_session.commands.blockers import (
    add_blocker_parsers,
    handle_permission_command,
    handle_question_command,
)
from opencode_session.commands.capabilities import add_capabilities_parser, handle_capabilities
from opencode_session.commands.sessions import (
    abort_record as _abort_record,
    add_session_parsers,
    format_abort_compact as _format_abort_compact,
    handle_session_command,
    is_session_not_found_error as _is_session_not_found_error,
)
from opencode_session.commands.validation import add_validation_parsers, handle_validation_command
from opencode_session.events import format_watch_event, is_abort_event, is_terminal_event, normalize_event
from opencode_session.formatting import (
    compact_value as _compact_value,
    write_raw as _write_raw,
)
from opencode_session.prompt_admission import (
    PromptAdmissionFailure,
    PromptAdmissionUnsupported,
    admit_prompt as admit_prompt_service,
)
from opencode_session.multi_worker_orchestration import (
    MultiWorkerRunOrchestrationService,
    MultiWorkerRunStartRequest,
    refresh_orchestration_run_summary as _refresh_orchestration_run_summary,
    save_orchestration_run as _save_orchestration_run,
    workers_in_dependency_order as _workers_in_dependency_order,
)
from opencode_session.run_store import RunStore, RunStoreError, default_store_root, format_run_compact
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService
from opencode_session.records import (
    session_value as _session_value,
)
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired as _WatchTimeout


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
CLI_NAME = "ocs"
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

    add_capabilities_parser(
        subparsers,
        add_server_argument=_add_server_argument,
        handler=partial(
            handle_capabilities,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
        ),
    )

    add_session_parsers(
        subparsers,
        add_server_argument=_add_server_argument,
        add_output_arguments=_add_output_arguments,
        handler=partial(
            handle_session_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
        ),
    )

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

    add_validation_parsers(
        subparsers,
        add_server_argument=_add_server_argument,
        handler=partial(
            handle_validation_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
            dataerr_exit=EX_DATAERR,
        ),
    )

    blocker_parsers = add_blocker_parsers(
        subparsers,
        add_server_argument=_add_server_argument,
        add_output_arguments=_add_output_arguments,
    )

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 64
    if args.command == "permission" and not args.permission_command:
        blocker_parsers["permission"].print_help(sys.stderr)
        return 64
    if args.command == "question" and not args.question_command:
        blocker_parsers["question"].print_help(sys.stderr)
        return 64
    command_handler = getattr(args, "command_handler", None)
    if command_handler is not None:
        return command_handler(args)
    if args.command == "run":
        return _handle_run_store_command(args)

    try:
        client = OpenCodeApiClient(args.server)
    except OpenCodeApiError as error:
        _print_error(str(error))
        return EX_UNAVAILABLE
    if args.command == "permission":
        return handle_permission_command(
            args,
            client,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            noinput_exit=EX_NOINPUT,
        )

    if args.command == "question":
        return handle_question_command(
            args,
            client,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            noinput_exit=EX_NOINPUT,
            dataerr_exit=EX_DATAERR,
        )

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

    if args.command == "watch":
        return _watch_session(args, client)

    if args.command == "steer":
        return _admit_prompt(args, client, args.delivery)

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
    outcome = MultiWorkerRunOrchestrationService(store).start(
        MultiWorkerRunStartRequest(
            name=args.name,
            worker_id=args.worker,
            role=args.role,
            directory=args.directory,
            server_url=args.server,
            session_id=args.session_id,
            cleanup=args.cleanup,
        )
    )
    if outcome.error is not None:
        _print_error(outcome.error)
        return outcome.exit_code
    print(format_run_compact(outcome.run))
    return outcome.exit_code


def _start_single_worker_run(args, store):
    outcome = SingleWorkerRunStateService(store).start(
        SingleWorkerRunStartRequest(
            name=args.name,
            worker_id=args.worker,
            role=args.role,
            prompt=args.prompt,
            directory=args.directory,
            server_url=args.server,
            session_id=args.session_id,
            agent=args.agent,
            model=args.model,
            cleanup=args.cleanup,
            default_server_url=_server_default(),
        )
    )
    if outcome.error is not None:
        _print_error(outcome.error)
        return outcome.exit_code
    print(format_run_compact(outcome.run))
    return outcome.exit_code


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
    try:
        result = admit_prompt_service(
            client,
            capabilities,
            worker["session_id"],
            args.text,
            args.delivery,
            message_id=args.message_id,
        )
    except PromptAdmissionUnsupported as error:
        _print_error(str(error))
        return EX_UNSUPPORTED
    except PromptAdmissionFailure as error:
        _print_error(str(error))
        return EX_UNAVAILABLE

    admission = result.record
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

    try:
        result = admit_prompt_service(
            client,
            capabilities,
            args.session_id,
            args.text,
            delivery,
            message_id=args.message_id,
        )
    except PromptAdmissionUnsupported as error:
        _print_error(str(error))
        return EX_UNSUPPORTED
    except PromptAdmissionFailure as error:
        _print_error(str(error))
        return EX_UNAVAILABLE

    if args.raw:
        _write_raw(result.body)
        return 0

    admission = result.record
    if args.json:
        print(json.dumps(admission, sort_keys=True))
    else:
        print(_format_admission_compact(admission))
    return 0


def _watch_session(args, client):
    pending_text = None
    deadline = TimeoutDeadline(args.timeout)
    event_deadline = deadline if args.timeout is not None else None

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
        try:
            capabilities = deadline.run(lambda: detect_capabilities(client))
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
            for raw_event in client.stream_events(event_route["path"], deadline=event_deadline):
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


def _read_prompt(prompt_words):
    if prompt_words:
        return " ".join(prompt_words)
    prompt = sys.stdin.read()
    if prompt.endswith("\n"):
        prompt = prompt[:-1]
    if prompt.endswith("\r"):
        prompt = prompt[:-1]
    return prompt


def _print_cleanup_error(error):
    _print_error(f"api failure: disposable session cleanup failed: {error}")


def _delete_disposable_session(client, session_id):
    if session_id is None:
        return None
    try:
        client.delete_session(session_id)
    except OpenCodeApiError as error:
        return error
    return None


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
