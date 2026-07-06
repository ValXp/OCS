from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import format_blocking_execution_compact as _format_run_compact
from opencode_session.cli_policy import server_default
from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.formatting import compact_value as _compact_value
from opencode_session.prompt_admission import (
    PromptAdmissionFailure,
    PromptAdmissionUnsupported,
    format_admission_compact,
)
from opencode_session.run_formatting import format_run_compact, format_worker_result_compact
from opencode_session.run_services import RunCommandService, RunStartRequest, RunWorkerSessionNotFound
from opencode_session.run_store import RunStore, RunStoreError, default_store_root
from opencode_session.session_lifecycle import format_abort_compact

def add_run_parser(subparsers, *, add_server_argument, positive_float, handler):
    parser = subparsers.add_parser("run", help="manage local orchestration runs")
    _add_run_store_arguments(parser, add_server_argument=add_server_argument, positive_float=positive_float)
    parser.set_defaults(command_handler=handler)


def handle_run_command(args, *, print_error, noinput_exit, dataerr_exit, unavailable_exit, unsupported_exit):
    store = RunStore(args.store)
    service = RunCommandService(store)
    try:
        handler = _RUN_HANDLERS.get(args.run_command)
        if handler is not None:
            return handler(
                args,
                service,
                print_error=print_error,
                unavailable_exit=unavailable_exit,
                unsupported_exit=unsupported_exit,
            )
    except RunStoreError as error:
        if error.kind == "missing":
            return _error_result(args, str(error), noinput_exit, print_error)
        return _error_result(args, str(error), dataerr_exit, print_error)
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
    run_start_parser.add_argument(
        "--execution-policy",
        choices=("fail-fast", "continue"),
        default="fail-fast",
        help="whether independent ready workers stop on the first failure or continue serially",
    )
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


def _start_orchestration_run(args, service, *, print_error, **_context):
    outcome = service.start_run(
        RunStartRequest(
            name=args.name,
            worker_id=args.worker,
            role=args.role,
            prompt=args.prompt,
            directory=args.directory,
            server_url=args.server,
            session_id=args.session_id,
            agent=args.agent,
            model=args.model,
            execution_policy=args.execution_policy,
            cleanup=args.cleanup,
            default_server_url=server_default(),
        )
    )
    if outcome.error is not None:
        return _error_result(args, outcome.error, outcome.exit_code, print_error)
    return render_command_result(args, CommandResult(outcome.run, compact=format_run_compact, exit_code=outcome.exit_code))


def _init_run(args, service, **_context):
    run = service.create_run(args.name, directory=args.directory, server_url=args.server)
    return render_command_result(args, run, compact=format_run_compact(run))


def _status_run(args, service, **_context):
    run = service.load_run(args.name)
    return render_command_result(args, run, compact=format_run_compact(run))


def _upsert_run_worker(args, service, **_context):
    run = service.upsert_worker(
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
    return render_command_result(args, run, compact=format_run_compact(run))


def _collect_run_results(args, service, **_context):
    collection = service.collect_results(args.name, worker_id=args.worker)
    if collection.worker is not None:
        return _print_single_worker_result(args, collection.worker)
    data = [
        {"worker": worker.get("id"), "role": worker.get("role"), "result": worker.get("result")}
        for worker in collection.workers
    ]
    return render_command_result(
        args,
        CommandResult(data, compact="\n".join(format_worker_result_compact(worker) for worker in collection.workers)),
    )


def _print_single_worker_result(args, worker):
    result = worker.get("result")
    return render_command_result(args, result, compact=_format_run_compact(result))


def _steer_run_worker(args, service, *, print_error, unavailable_exit, unsupported_exit):
    try:
        result = service.steer_worker(
            args.name,
            args.worker_id,
            args.text,
            delivery=args.delivery,
            message_id=args.message_id,
        )
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)
    except PromptAdmissionUnsupported as error:
        return _error_result(args, str(error), unsupported_exit, print_error)
    except PromptAdmissionFailure as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    run = result.run
    worker = result.worker
    admission = result.admission
    return render_command_result(
        args,
        {"run": run["name"], "worker": worker["id"], "admission": admission},
        compact=f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {format_admission_compact(admission)}",
    )


def _abort_run_worker(args, service, *, print_error, unavailable_exit, **_context):
    try:
        result = service.abort_worker(args.name, args.worker_id)
    except RunWorkerSessionNotFound as error:
        return _error_result(args, str(error), unavailable_exit, print_error)
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)
    run = result.run
    worker = result.worker
    abort = result.abort
    return render_command_result(
        args,
        {"run": run["name"], "worker": worker["id"], "abort": abort},
        compact=f"run={_compact_value(run['name'])} worker={_compact_value(worker['id'])} {format_abort_compact(abort)}",
    )


def _positive_timeout_seconds(value, positive_float):
    timeout = positive_float(value)
    if timeout.is_integer():
        return int(timeout)
    return timeout


def _error_result(args, message, exit_code, print_error):
    return render_command_result(args, CommandResult(error=message, exit_code=exit_code), print_error=print_error)


_RUN_HANDLERS = {
    "start": _start_orchestration_run,
    "init": _init_run,
    "status": _status_run,
    "collect": _collect_run_results,
    "worker": _upsert_run_worker,
    "steer": _steer_run_worker,
    "abort": _abort_run_worker,
}
