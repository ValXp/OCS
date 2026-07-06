import sys
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.blocking_execution import (
    BlockingProviderFailure,
    blocking_execution_strategy,
    execute_blocking_prompt,
    format_blocking_execution_compact,
    unsupported_blocking_execution_message,
)
from opencode_session.capabilities import capabilities_from_openapi_doc, configure_client_route_plan
from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.disposable_session_lifecycle import delete_and_verify_disposable_session
from opencode_session.session_ids import require_session_id
from opencode_session.session_lifecycle import is_session_not_found_error


def add_run_blocking_parser(subparsers, *, add_server_argument, handler):
    parser = subparsers.add_parser(
        "run_blocking",
        help="execute a task and wait for an assistant reply",
        description="Execute a task and wait for an assistant reply or terminal failure.",
    )
    parser.add_argument("prompt", nargs="*", help="prompt text; stdin is used when omitted")
    parser.add_argument("--session", help="existing session ID to run in")
    parser.add_argument("--directory", help="target directory when creating a disposable session")
    parser.add_argument("--agent", help="agent name for a disposable session")
    parser.add_argument("--model", help="model name for a disposable session")
    add_server_argument(parser)
    parser.add_argument("--json", action="store_true", help="print normalized JSON result")
    parser.set_defaults(command_handler=handler)


def handle_run_blocking_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    client_factory=OpenCodeApiClient,
):
    prompt = _read_prompt(args.prompt)
    session_id = args.session
    created_session_id = None
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    try:
        capabilities = capabilities_from_openapi_doc(client.require_openapi_doc())
        configure_client_route_plan(client, capabilities)
        if blocking_execution_strategy(capabilities) is None:
            return _error_result(args, unsupported_blocking_execution_message(), unsupported_exit, print_error)
        if session_id is None:
            directory = str(Path(args.directory or ".").resolve())
            create_response = client.create_session_response(
                directory,
                agent=args.agent,
                model=args.model,
            )
            session_id = require_session_id(create_response)
            created_session_id = session_id
        result = execute_blocking_prompt(client, session_id, prompt, capabilities)
    except BlockingProviderFailure as error:
        cleanup_error = _delete_disposable_session(client, created_session_id)
        return _error_result(
            args,
            f"provider failure: {error}",
            unavailable_exit,
            print_error,
            warnings=_cleanup_warnings(cleanup_error),
        )
    except OpenCodeApiError as error:
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if session_id is not None and is_session_not_found_error(error):
            return _error_result(
                args,
                f"session not found: {session_id}",
                unavailable_exit,
                print_error,
                warnings=_cleanup_warnings(cleanup_error),
            )
        else:
            return _error_result(
                args,
                f"api failure: {error}",
                unavailable_exit,
                print_error,
                warnings=_cleanup_warnings(cleanup_error),
            )
    cleanup_error = _delete_disposable_session(client, created_session_id)
    if cleanup_error:
        return _error_result(args, f"api failure: disposable session cleanup failed: {cleanup_error}", unavailable_exit, print_error)
    return render_command_result(args, CommandResult(result, compact=format_blocking_execution_compact))


def _read_prompt(prompt_words):
    if prompt_words:
        return " ".join(prompt_words)
    prompt = sys.stdin.read()
    if prompt.endswith("\n"):
        prompt = prompt[:-1]
    if prompt.endswith("\r"):
        prompt = prompt[:-1]
    return prompt


def _error_result(args, message, exit_code, print_error, *, warnings=()):
    return render_command_result(
        args,
        CommandResult(error=message, exit_code=exit_code, warnings=warnings),
        print_error=print_error,
    )


def _cleanup_warnings(error):
    if error is None:
        return ()
    return (f"api failure: disposable session cleanup failed: {error}",)


def _delete_disposable_session(client, session_id):
    if session_id is None:
        return None
    return delete_and_verify_disposable_session(client, session_id)
