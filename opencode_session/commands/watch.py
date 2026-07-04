import json

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities
from opencode_session.events import format_watch_event, is_abort_event, is_terminal_event, normalize_event
from opencode_session.timeout_boundary import TimeoutDeadline, TimeoutExpired


def add_watch_parser(subparsers, *, add_server_argument, positive_float, handler):
    parser = subparsers.add_parser("watch", help="watch session progress events")
    parser.add_argument("session_id", help="session ID to watch")
    add_server_argument(parser)
    parser.add_argument("--json", action="store_true", help="print normalized event JSON lines")
    parser.add_argument("--timeout", type=positive_float, help="stop watching after this many seconds")
    parser.set_defaults(command_handler=handler)


def handle_watch_command(
    args,
    *,
    print_error,
    unavailable_exit,
    unsupported_exit,
    dataerr_exit,
    timeout_exit,
    aborted_exit,
    client_factory=OpenCodeApiClient,
):
    try:
        client = client_factory(args.server)
    except OpenCodeApiError as error:
        print_error(str(error))
        return unavailable_exit
    return _watch_session(
        args,
        client,
        print_error=print_error,
        unavailable_exit=unavailable_exit,
        unsupported_exit=unsupported_exit,
        dataerr_exit=dataerr_exit,
        timeout_exit=timeout_exit,
        aborted_exit=aborted_exit,
    )


def _watch_session(args, client, *, print_error, unavailable_exit, unsupported_exit, dataerr_exit, timeout_exit, aborted_exit):
    pending_text = None
    deadline = TimeoutDeadline(args.timeout)
    event_deadline = deadline if args.timeout is not None else None

    def flush_pending_text():
        nonlocal pending_text
        if pending_text is not None:
            print(format_watch_event(pending_text), flush=True)
            pending_text = None

    def emit_event(event):
        nonlocal pending_text
        if args.json:
            print(json.dumps(event, sort_keys=True), flush=True)
            return
        if event["kind"] == "text":
            if pending_text is not None and _same_watch_text_group(pending_text, event):
                pending_text = dict(pending_text)
                pending_text["text"] = (pending_text.get("text") or "") + (event.get("text") or "")
            else:
                flush_pending_text()
                pending_text = dict(event)
            return
        flush_pending_text()
        print(format_watch_event(event), flush=True)

    try:
        try:
            capabilities = deadline.run(lambda: detect_capabilities(client))
        except OpenCodeApiError as error:
            print_error(str(error))
            return unavailable_exit

        event_route = capabilities["route_availability"]["events"]
        if not event_route["available"]:
            print_error("unsupported OpenCode server; missing event stream: GET /api/event or GET /event or GET /global/event")
            return unsupported_exit

        try:
            for raw_event in client.stream_events(event_route["path"], deadline=event_deadline):
                event = normalize_event(raw_event, args.session_id)
                if event is None:
                    continue
                emit_event(event)
                if is_terminal_event(event):
                    flush_pending_text()
                    if is_abort_event(event):
                        return aborted_exit
                    return 0
        except OpenCodeApiError as error:
            flush_pending_text()
            print_error(f"event stream failure: {error}")
            if _is_invalid_event_stream(error):
                return dataerr_exit
            return unavailable_exit
        flush_pending_text()
        return 0
    except TimeoutExpired:
        flush_pending_text()
        print_error(f"watch timed out after {_format_timeout(args.timeout)}s")
        return timeout_exit


def _same_watch_text_group(left, right):
    return left.get("session_id") == right.get("session_id") and left.get("message_id") == right.get("message_id")


def _is_invalid_event_stream(error):
    return isinstance(error.data, dict) and error.data.get("kind") == "invalid_event_stream"


def _format_timeout(timeout):
    return str(timeout)
