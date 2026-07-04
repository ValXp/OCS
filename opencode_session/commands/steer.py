import json

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities
from opencode_session.formatting import write_raw
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
        print_error(str(error))
        return unavailable_exit

    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

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
        print_error(str(error))
        return unsupported_exit
    except PromptAdmissionFailure as error:
        print_error(str(error))
        return unavailable_exit

    if args.raw:
        write_raw(result.body)
        return 0

    admission = result.record
    if args.json:
        print(json.dumps(admission, sort_keys=True))
    else:
        print(format_admission_compact(admission))
    return 0
