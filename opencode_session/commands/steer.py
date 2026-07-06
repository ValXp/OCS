from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.capabilities import detect_capabilities
from opencode_session.commands.rendering import CommandResult, render_command_result
from opencode_session.prompt_admission import (
    PromptAdmissionFailure,
    PromptAdmissionUnsupported,
    admit_prompt,
    format_admission_compact,
)


def add_steer_parser(subparsers, *, add_server_argument, add_output_arguments, handler):
    parser = subparsers.add_parser(
        "steer",
        help="admit durable input to a session",
        description="Admit steer or queue input to a session and report admission/progress state; does not wait for an assistant reply.",
    )
    parser.add_argument("session_id", help="session ID to admit input to")
    parser.add_argument("text", help="input text to admit")
    parser.add_argument(
        "--delivery",
        choices=("steer", "queue"),
        default="steer",
        help="admission delivery mode; queue admits input without competing as a top-level command",
    )
    parser.add_argument("--message-id", help="client-supplied prompt/message ID for idempotent admission")
    add_server_argument(parser)
    add_output_arguments(parser)
    parser.set_defaults(command_handler=handler)


def handle_steer_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    try:
        result = admit_prompt(
            client,
            capabilities,
            args.session_id,
            args.text,
            args.delivery,
            message_id=args.message_id,
        )
    except PromptAdmissionUnsupported as error:
        return _error_result(args, str(error), unsupported_exit, print_error)
    except PromptAdmissionFailure as error:
        return _error_result(args, str(error), unavailable_exit, print_error)

    admission = result.record
    return render_command_result(
        args,
        CommandResult(admission, raw_body=result.body, compact=format_admission_compact),
    )


def _error_result(args, message, exit_code, print_error):
    return render_command_result(args, CommandResult(error=message, exit_code=exit_code), print_error=print_error)
