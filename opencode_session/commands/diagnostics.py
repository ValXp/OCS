from opencode_session.api_client import OpenCodeApiClient
from opencode_session.api_transport import OpenCodeApiError
from opencode_session.commands.rendering import render_command_result
from opencode_session.diagnostics import (
    ApiDiagnosticsError,
    ApiDiagnosticsService,
    format_diagnostics_compact,
    format_routes_compact,
)


def add_diagnostics_parser(subparsers, *, add_server_argument, add_output_arguments, handler):
    parser = subparsers.add_parser("diagnostics", help="inspect read-only OpenCode routes and API responses")
    diagnostics_subparsers = parser.add_subparsers(dest="diagnostics_command")
    diagnostics_subparsers.required = True

    routes_parser = diagnostics_subparsers.add_parser("routes", help="list OpenAPI routes")
    routes_parser.add_argument("--filter", dest="filter_text", help="only show matching paths or methods")
    routes_parser.add_argument("--json", action="store_true", help="print route records as JSON")
    add_server_argument(routes_parser)
    routes_parser.set_defaults(command_handler=handler)

    get_parser = diagnostics_subparsers.add_parser("get", help="GET an advertised read-only API path")
    get_parser.add_argument("path", help="same-server absolute API path beginning with /")
    add_server_argument(get_parser)
    add_output_arguments(get_parser)
    get_parser.set_defaults(command_handler=handler)
    return parser


def handle_diagnostics_command(
    args,
    *,
    print_error,
    unavailable_exit,
    dataerr_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        service = ApiDiagnosticsService(client_factory(args.server))
        if args.diagnostics_command == "routes":
            routes = service.list_routes(filter_text=args.filter_text)
            return render_command_result(args, routes, compact=format_routes_compact(routes))
        if args.diagnostics_command == "get":
            response = service.get(args.path)
            return render_command_result(
                args,
                response.data,
                raw_body=response.body,
                compact=format_diagnostics_compact(response.data),
            )
    except ApiDiagnosticsError as error:
        print_error(str(error))
        return dataerr_exit
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    return 64
