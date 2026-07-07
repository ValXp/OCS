import argparse

from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.formatting import compact_value
from opencode_session.validation_cleanup import cleanup_stale_disposable_sessions
from opencode_session.validation_live import (
    LIVE_SESSION_PREFIX,
    LIVE_VALIDATE_ENV,
    live_validate_env_failure,
    run_live_validate,
)
from opencode_session.validation_smoke import SMOKE_EVENT_TIMEOUT_SECONDS, SMOKE_SESSION_PREFIX, run_smoke


def add_validation_parsers(subparsers, *, add_server_argument, handler):
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
    add_server_argument(smoke_parser)
    smoke_parser.add_argument("--json", action="store_true", help="print smoke result JSON")
    smoke_parser.set_defaults(command_handler=handler)

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
    add_server_argument(live_parser)
    live_parser.add_argument("--json", action="store_true", help="print live validation result JSON")
    live_parser.set_defaults(command_handler=handler)

    cleanup_parser = subparsers.add_parser("cleanup", help="delete stale disposable smoke sessions")
    cleanup_parser.add_argument("--directory", default=".", help="target directory to clean")
    cleanup_parser.add_argument("--prefix", default=SMOKE_SESSION_PREFIX, help="disposable session prefix to match")
    add_server_argument(cleanup_parser)
    cleanup_parser.add_argument("--json", action="store_true", help="print cleanup result JSON")
    cleanup_parser.set_defaults(command_handler=handler)


def handle_validation_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    dataerr_exit,
    client_factory=OpenCodeApiClient,
):
    if args.command not in {"smoke", "live_validate", "cleanup"}:
        return 64
    if args.command == "live_validate":
        env_failure = live_validate_env_failure(print_error, dataerr_exit)
        if env_failure is not None:
            return env_failure
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    if args.command == "smoke":
        return run_smoke(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            unsupported_exit=unsupported_exit,
        )
    if args.command == "live_validate":
        return run_live_validate(
            args,
            client,
            print_error=print_error,
            unavailable_exit=unavailable_exit,
            unsupported_exit=unsupported_exit,
        )
    return cleanup_disposable_command(args, client, print_error=print_error, unavailable_exit=unavailable_exit)


def cleanup_disposable_command(args, client, *, print_error, unavailable_exit):
    try:
        result = cleanup_stale_disposable_sessions(client, prefix=args.prefix, directory=args.directory)
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    if result["status"] != "done":
        message = f"cleanup failed: {format_cleanup_command_compact(result)}"
        return _error_result(args, message, unavailable_exit, print_error)
    return render_command_result(args, CommandResult(result, compact=format_cleanup_command_compact))


def _error_result(args, message, exit_code, print_error):
    return render_command_result(args, CommandResult(error=message, exit_code=exit_code), print_error=print_error)


def format_cleanup_command_compact(result):
    fields = [
        ("stale", result["stale"]),
        ("deleted", len(result["deleted"])),
        ("verified", len(result["verified"])),
        ("prefix", result["prefix"]),
        ("dir", result["directory"]),
    ]
    return "cleanup " + " ".join(f"{key}={compact_value(value)}" for key, value in fields)


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
