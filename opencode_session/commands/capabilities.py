import json

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities, format_compact, unsupported_reasons


def add_capabilities_parser(subparsers, *, add_server_argument, handler):
    parser = subparsers.add_parser("capabilities", help="probe OpenCode API capabilities")
    add_server_argument(parser)
    parser.add_argument("--json", action="store_true", help="print full JSON capability data")
    parser.set_defaults(command_handler=handler)
    return parser


def handle_capabilities(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        client = client_factory(args.server)
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit

    reasons = unsupported_reasons(capabilities)
    if reasons:
        print_error(f"unsupported OpenCode server; {'; '.join(reasons)}")
        return unsupported_exit

    if args.json:
        print(json.dumps(capabilities, sort_keys=True))
    else:
        print(format_compact(capabilities))
    return 0
