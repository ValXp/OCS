import json
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
from opencode_session.capabilities import capabilities_from_openapi_doc
from opencode_session.records import session_value
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
        print_error(str(error))
        return unavailable_exit

    try:
        capabilities = capabilities_from_openapi_doc(client.require_openapi_doc())
        if blocking_execution_strategy(capabilities) is None:
            print_error(unsupported_blocking_execution_message())
            return unsupported_exit
        if session_id is None:
            directory = str(Path(args.directory or ".").resolve())
            create_response = client.create_session_response(
                directory,
                agent=args.agent,
                model=args.model,
            )
            session_id = session_value(create_response.data, "id", "sessionID", "sessionId")
            created_session_id = session_id
        result = execute_blocking_prompt(client, session_id, prompt, capabilities)
    except BlockingProviderFailure as error:
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if cleanup_error:
            _print_cleanup_error(print_error, cleanup_error)
        print_error(f"provider failure: {error}")
        return unavailable_exit
    except OpenCodeApiError as error:
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if cleanup_error:
            _print_cleanup_error(print_error, cleanup_error)
        if session_id is not None and is_session_not_found_error(error):
            print_error(f"session not found: {session_id}")
        else:
            print_error(f"api failure: {error}")
        return unavailable_exit
    cleanup_error = _delete_disposable_session(client, created_session_id)
    if cleanup_error:
        _print_cleanup_error(print_error, cleanup_error)
        return unavailable_exit
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    print(format_blocking_execution_compact(result))
    return 0


def _read_prompt(prompt_words):
    if prompt_words:
        return " ".join(prompt_words)
    prompt = sys.stdin.read()
    if prompt.endswith("\n"):
        prompt = prompt[:-1]
    if prompt.endswith("\r"):
        prompt = prompt[:-1]
    return prompt


def _print_cleanup_error(print_error, error):
    print_error(f"api failure: disposable session cleanup failed: {error}")


def _delete_disposable_session(client, session_id):
    if session_id is None:
        return None
    try:
        client.delete_session(session_id)
    except OpenCodeApiError as error:
        return error
    return None
