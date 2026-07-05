import json
import os
from datetime import datetime, timezone
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import format_blocking_execution_compact as _format_run_compact
from opencode_session.capabilities import detect_capabilities
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.multi_worker_orchestration import (
    MultiWorkerRunOrchestrationService,
    MultiWorkerRunStartRequest,
    refresh_orchestration_run_summary as _refresh_orchestration_run_summary,
    workers_in_dependency_order as _workers_in_dependency_order,
)
from opencode_session.prompt_admission import (
    PromptAdmissionFailure,
    PromptAdmissionUnsupported,
    admit_prompt,
    format_admission_compact,
)
from opencode_session.records import tokens_total as _tokens_total
from opencode_session.run_formatting import format_run_compact
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService
from opencode_session.run_store import RunStore, RunStoreError, default_store_root
from opencode_session.session_lifecycle import abort_record, format_abort_compact, is_session_not_found_error
from opencode_session.worker_state import mark_worker_aborted as _mark_worker_aborted


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"


def add_run_parser(subparsers, *, add_server_argument, positive_float, handler):
    parser = subparsers.add_parser("run", help="manage local orchestration runs")
    _add_run_store_arguments(parser, add_server_argument=add_server_argument, positive_float=positive_float)
    parser.set_defaults(command_handler=handler)


def handle_run_command(args, *, print_error, noinput_exit, dataerr_exit, unavailable_exit, unsupported_exit):
    store = RunStore(args.store)
    try:
        if args.run_command == "start":
            return _start_orchestration_run(args, store, print_error=print_error)
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
            return _steer_run_worker(
                args,
                store,
                print_error=print_error,
                unavailable_exit=unavailable_exit,
                unsupported_exit=unsupported_exit,
            )
        if args.run_command == "abort":
            return _abort_run_worker(args, store, print_error=print_error, unavailable_exit=unavailable_exit)
    except RunStoreError as error:
        print_error(str(error))
        if error.kind == "missing":
            return noinput_exit
        return dataerr_exit
    return 64


def _add_run_store_arguments(parser, *, add_server_argument, positive_float):
    parser.add_argument("--store", default=default_store_root(), help="local orchestration run store directory")
    run_subparsers = parser.add_subparsers(dest="run_command")
    run_subparsers.required = True

    run_init_parser = run_subparsers.add_parser("init")
    run_init_parser.add_argument("name", help="local run name")
    run_init_parser.add_argument("--directory", default=".", help="target directory for the run")
    add_server_argument(run_init_parser)

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
    run_worker_parser.add_argument(
        "--timeout-seconds",
        type=lambda value: _positive_timeout_seconds(value, positive_float),
        help="worker timeout in seconds",
    )
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


def _start_orchestration_run(args, store, *, print_error):
    if args.prompt is not None:
        return _start_single_worker_run(args, store, print_error=print_error)
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
        print_error(outcome.error)
        return outcome.exit_code
    print(format_run_compact(outcome.run))
    return outcome.exit_code


def _start_single_worker_run(args, store, *, print_error):
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
        print_error(outcome.error)
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


def _steer_run_worker(args, store, *, print_error, unavailable_exit, unsupported_exit):
    run = store.load_run(args.name)
    worker = _run_worker_with_session(run, args.worker_id)
    try:
        client = OpenCodeApiClient(run["server_url"])
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    try:
        result = admit_prompt(
            client,
            capabilities,
            worker["session_id"],
            args.text,
            args.delivery,
            message_id=args.message_id,
        )
    except PromptAdmissionUnsupported as error:
        print_error(str(error))
        return unsupported_exit
    except PromptAdmissionFailure as error:
        print_error(str(error))
        return unavailable_exit

    admission = result.record
    message_id = admission["message_id"]

    def append_prompt_id(latest_run):
        latest_worker = _run_worker_with_session(latest_run, args.worker_id)
        prompt_ids = latest_worker.setdefault("prompt_ids", [])
        if message_id not in prompt_ids:
            prompt_ids.append(message_id)

    run = _update_orchestration_run(store, args.name, append_prompt_id)
    worker = run["workers"][args.worker_id]
    if args.json:
        print(json.dumps({"run": run["name"], "worker": worker["id"], "admission": admission}, sort_keys=True))
    else:
        print(f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {format_admission_compact(admission)}")
    return 0


def _abort_run_worker(args, store, *, print_error, unavailable_exit):
    run = store.load_run(args.name)
    worker = _run_worker_with_session(run, args.worker_id)
    try:
        client = OpenCodeApiClient(run["server_url"])
        response = client.abort_session_response(worker["session_id"])
    except OpenCodeApiError as error:
        if is_session_not_found_error(error):
            print_error(f"session not found: {worker['session_id']}")
        else:
            print_error(str(error))
        return unavailable_exit
    abort = abort_record(worker["session_id"], response.data)

    def mark_aborted(latest_run):
        latest_worker = _run_worker_with_session(latest_run, args.worker_id)
        _mark_worker_aborted(latest_worker, abort)
        _refresh_orchestration_run_summary(latest_run)

    run = _update_orchestration_run(store, args.name, mark_aborted)
    worker = run["workers"][args.worker_id]
    if args.json:
        print(json.dumps({"run": run["name"], "worker": worker["id"], "abort": abort}, sort_keys=True))
    else:
        print(f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {format_abort_compact(abort)}")
    return 0


def _update_orchestration_run(store, name, mutator):
    def update(run):
        mutator(run)
        run["updated_at"] = _utc_now()

    return store.update_run(name, update)


def _run_worker_with_session(run, worker_id):
    worker = run.get("workers", {}).get(worker_id)
    if not isinstance(worker, dict):
        raise RunStoreError(f"worker '{worker_id}' not found in run '{run['name']}'", kind="missing")
    if not worker.get("session_id"):
        raise RunStoreError(f"worker '{worker_id}' in run '{run['name']}' has no session", kind="missing")
    return worker


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _positive_timeout_seconds(value, positive_float):
    timeout = positive_float(value)
    if timeout.is_integer():
        return int(timeout)
    return timeout
