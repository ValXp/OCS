import argparse
import sys
from dataclasses import dataclass
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
from opencode_session.commands.diagnostics import add_diagnostics_parser, handle_diagnostics_command
from opencode_session.commands.run_blocking import add_run_blocking_parser, handle_run_blocking_command
from opencode_session.commands.runs import add_run_parser, handle_run_command
from opencode_session.commands.sessions import add_session_parsers, handle_session_command
from opencode_session.commands.steer import add_steer_parser, handle_steer_command
from opencode_session.commands.validation import add_validation_parsers, handle_validation_command
from opencode_session.commands.watch import add_watch_parser, handle_watch_command


@dataclass(frozen=True)
class CommandSpec:
    add_parser: object
    handler: object
    parser_kwargs: dict
    handler_kwargs: dict


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    parser = argparse.ArgumentParser(prog=CLI_NAME, description="Agent-friendly OpenCode session CLI.")
    subparsers = parser.add_subparsers(dest="command")

    for command in _command_specs():
        command.add_parser(
            subparsers,
            **command.parser_kwargs,
            handler=partial(command.handler, print_error=_print_error, **command.handler_kwargs),
        )

    args = parser.parse_args(argv)
    command_handler = getattr(args, "command_handler", None)
    if command_handler is None:
        parser.print_help(sys.stderr)
        return EX_USAGE
    return command_handler(args)


def _print_error(message):
    print(f"{CLI_NAME}: {message}", file=sys.stderr)


def _command_specs():
    return (
        CommandSpec(
            add_capabilities_parser,
            handle_capabilities,
            {"add_server_argument": _add_server_argument},
            {"unavailable_exit": EX_UNAVAILABLE, "unsupported_exit": EX_UNSUPPORTED},
        ),
        CommandSpec(
            add_session_parsers,
            handle_session_command,
            {"add_server_argument": _add_server_argument, "add_output_arguments": _add_output_arguments},
            {"unavailable_exit": EX_UNAVAILABLE},
        ),
        CommandSpec(
            add_diagnostics_parser,
            handle_diagnostics_command,
            {"add_server_argument": _add_server_argument, "add_output_arguments": _add_output_arguments},
            {"unavailable_exit": EX_UNAVAILABLE, "dataerr_exit": EX_DATAERR},
        ),
        CommandSpec(
            add_watch_parser,
            handle_watch_command,
            {"add_server_argument": _add_server_argument, "positive_float": _positive_float},
            {
                "unavailable_exit": EX_UNAVAILABLE,
                "unsupported_exit": EX_UNSUPPORTED,
                "dataerr_exit": EX_DATAERR,
                "timeout_exit": EX_TIMEOUT,
                "aborted_exit": EX_ABORTED,
            },
        ),
        CommandSpec(
            add_run_parser,
            handle_run_command,
            {"add_server_argument": _add_server_argument, "positive_float": _positive_float},
            {
                "noinput_exit": EX_NOINPUT,
                "dataerr_exit": EX_DATAERR,
                "unavailable_exit": EX_UNAVAILABLE,
                "unsupported_exit": EX_UNSUPPORTED,
            },
        ),
        CommandSpec(
            add_run_blocking_parser,
            handle_run_blocking_command,
            {"add_server_argument": _add_server_argument},
            {"unavailable_exit": EX_UNAVAILABLE, "unsupported_exit": EX_UNSUPPORTED},
        ),
        CommandSpec(
            add_steer_parser,
            handle_steer_command,
            {"add_server_argument": _add_server_argument, "add_output_arguments": _add_output_arguments},
            {"unavailable_exit": EX_UNAVAILABLE, "unsupported_exit": EX_UNSUPPORTED},
        ),
        CommandSpec(
            add_validation_parsers,
            handle_validation_command,
            {"add_server_argument": _add_server_argument},
            {
                "unavailable_exit": EX_UNAVAILABLE,
                "unsupported_exit": EX_UNSUPPORTED,
                "dataerr_exit": EX_DATAERR,
            },
        ),
        CommandSpec(
            add_blocker_parsers,
            handle_blocker_command,
            {"add_server_argument": _add_server_argument, "add_output_arguments": _add_output_arguments},
            {"unavailable_exit": EX_UNAVAILABLE, "noinput_exit": EX_NOINPUT, "dataerr_exit": EX_DATAERR},
        ),
    )


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
