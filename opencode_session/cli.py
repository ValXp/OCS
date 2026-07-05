import argparse
import sys
from functools import partial

from opencode_session.cli_policy import (
    CLI_NAME,
    EX_ABORTED,
    EX_DATAERR,
    EX_NOINPUT,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    EX_USAGE,
    server_default,
)
from opencode_session.commands.blockers import add_blocker_parsers, handle_blocker_command
from opencode_session.commands.capabilities import add_capabilities_parser, handle_capabilities
from opencode_session.commands.run_blocking import add_run_blocking_parser, handle_run_blocking_command
from opencode_session.commands.runs import add_run_parser, handle_run_command
from opencode_session.commands.sessions import add_session_parsers, handle_session_command
from opencode_session.commands.steer import add_steer_parser, handle_steer_command
from opencode_session.commands.validation import add_validation_parsers, handle_validation_command
from opencode_session.commands.watch import add_watch_parser, handle_watch_command

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

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

    add_watch_parser(
        subparsers,
        add_server_argument=_add_server_argument,
        positive_float=_positive_float,
        handler=partial(
            handle_watch_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
            dataerr_exit=EX_DATAERR,
            timeout_exit=EX_TIMEOUT,
            aborted_exit=EX_ABORTED,
        ),
    )

    add_run_parser(
        subparsers,
        add_server_argument=_add_server_argument,
        positive_float=_positive_float,
        handler=partial(
            handle_run_command,
            print_error=_print_error,
            noinput_exit=EX_NOINPUT,
            dataerr_exit=EX_DATAERR,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
        ),
    )

    add_run_blocking_parser(
        subparsers,
        add_server_argument=_add_server_argument,
        handler=partial(
            handle_run_blocking_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
        ),
    )

    add_steer_parser(
        subparsers,
        add_server_argument=_add_server_argument,
        add_output_arguments=_add_output_arguments,
        handler=partial(
            handle_steer_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            unsupported_exit=EX_UNSUPPORTED,
        ),
    )

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

    add_blocker_parsers(
        subparsers,
        add_server_argument=_add_server_argument,
        add_output_arguments=_add_output_arguments,
        handler=partial(
            handle_blocker_command,
            print_error=_print_error,
            unavailable_exit=EX_UNAVAILABLE,
            noinput_exit=EX_NOINPUT,
            dataerr_exit=EX_DATAERR,
        ),
    )

    args = parser.parse_args(argv)
    command_handler = getattr(args, "command_handler", None)
    if command_handler is None:
        parser.print_help(sys.stderr)
        return EX_USAGE
    return command_handler(args)


def _print_error(message):
    print(f"{CLI_NAME}: {message}", file=sys.stderr)


def _add_server_argument(parser):
    parser.add_argument(
        "--server",
        default=server_default(),
        help="OpenCode server URL",
    )


def _add_output_arguments(parser):
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print JSON data")
    output.add_argument("--raw", action="store_true", help="print raw API response body")


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number
