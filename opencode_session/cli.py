import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities, format_compact, unsupported_reasons


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opencode-session")
    subparsers = parser.add_subparsers(dest="command")

    capabilities_parser = subparsers.add_parser("capabilities")
    _add_server_argument(capabilities_parser)
    capabilities_parser.add_argument("--json", action="store_true", help="print full JSON capability data")

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("directory", help="target directory for the new session")
    create_parser.add_argument("--agent", help="agent name for the new session")
    create_parser.add_argument("--model", help="model name for the new session")
    _add_server_argument(create_parser)
    _add_output_arguments(create_parser)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--directory", help="only show sessions for this target directory")
    list_parser.add_argument("--agent", help="only show sessions for this agent")
    list_parser.add_argument("--model", help="only show sessions for this model")
    _add_server_argument(list_parser)
    _add_output_arguments(list_parser)

    for name in ("inspect", "get"):
        inspect_parser = subparsers.add_parser(name)
        inspect_parser.add_argument("session_id", help="session ID to inspect")
        _add_server_argument(inspect_parser)
        _add_output_arguments(inspect_parser)

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("session_id", help="session ID to delete")
    _add_server_argument(delete_parser)
    _add_output_arguments(delete_parser)

    steer_parser = subparsers.add_parser("steer")
    _add_admission_arguments(steer_parser)

    queue_parser = subparsers.add_parser("queue")
    _add_admission_arguments(queue_parser)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 64

    client = OpenCodeApiClient(args.server)
    if args.command == "create":
        directory = str(Path(args.directory).resolve())
        try:
            response = client.create_session_response(directory, agent=args.agent, model=args.model)
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        session = response.data
        if args.json:
            print(json.dumps(session, sort_keys=True))
            return 0
        print(_format_session_compact(session))
        return 0

    if args.command == "list":
        try:
            response = client.list_sessions_response()
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        collection = response.data
        directory = str(Path(args.directory).resolve()) if args.directory else None
        sessions = _filter_sessions(_collection_sessions(collection), directory=directory, agent=args.agent, model=args.model)
        if args.json:
            print(json.dumps(sessions, sort_keys=True))
            return 0
        if sessions:
            print("\n".join(_format_session_compact(session) for session in sessions))
        return 0

    if args.command in ("inspect", "get"):
        try:
            response = client.get_session_response(args.session_id)
        except OpenCodeApiError as error:
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        if args.raw:
            _write_raw(response.body)
            return 0
        session = response.data
        if args.json:
            print(json.dumps(session, sort_keys=True))
            return 0
        print(_format_session_compact(session))
        return 0

    if args.command == "delete":
        delete_response = None
        deleted = False
        try:
            delete_response = client.delete_session_response(args.session_id)
            deleted = True
            client.get_session(args.session_id)
        except OpenCodeApiError as error:
            if deleted and error.status == 404:
                if args.raw:
                    _write_raw(delete_response.body if delete_response else "")
                    return 0
                if args.json:
                    print(
                        json.dumps(
                            {
                                "deleted": True,
                                "id": args.session_id,
                                "response": delete_response.data if delete_response else None,
                                "verified": "unreadable",
                            },
                            sort_keys=True,
                        )
                    )
                    return 0
                print(f"deleted id={_compact_value(args.session_id)} verified=unreadable")
                return 0
            print(f"opencode-session: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        print(f"opencode-session: delete verification failed; session {args.session_id} is still readable", file=sys.stderr)
        return EX_UNAVAILABLE

    if args.command == "steer":
        return _admit_prompt(args, client, "steer")

    if args.command == "queue":
        return _admit_prompt(args, client, "queue")

    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        return EX_UNAVAILABLE

    reasons = unsupported_reasons(capabilities)
    if reasons:
        print(f"opencode-session: unsupported OpenCode server; {'; '.join(reasons)}", file=sys.stderr)
        return EX_UNSUPPORTED

    if args.json:
        print(json.dumps(capabilities, sort_keys=True))
    else:
        print(format_compact(capabilities))
    return 0


def _add_server_argument(parser):
    parser.add_argument(
        "--server",
        default=os.environ.get("OPENCODE_SERVER_URL")
        or os.environ.get("OPENCODE_SERVER")
        or DEFAULT_SERVER_URL,
        help="OpenCode server URL",
    )


def _add_output_arguments(parser):
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print complete JSON data")
    output.add_argument("--raw", action="store_true", help="print raw API response body")


def _add_admission_arguments(parser):
    parser.add_argument("session_id", help="session ID to admit input to")
    parser.add_argument("text", help="prompt text to admit")
    parser.add_argument("--message-id", help="client-supplied prompt/message ID for idempotent admission")
    _add_server_argument(parser)
    _add_output_arguments(parser)


def _admit_prompt(args, client, delivery):
    try:
        capabilities = detect_capabilities(client)
    except OpenCodeApiError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        return EX_UNAVAILABLE

    if not capabilities["v2_prompt_support"]:
        print(
            "opencode-session: unsupported v2 prompt capability; durable prompt admission requires "
            "POST /api/session/{sessionID}/prompt or POST /session/{sessionID}/prompt_async; "
            "legacy run/reply fallback is not used for steer/queue admission",
            file=sys.stderr,
        )
        return EX_UNSUPPORTED

    message_id = args.message_id or f"msg_{uuid.uuid4().hex}"
    payload = {
        "messageID": message_id,
        "parts": [{"type": "text", "text": args.text}],
        "delivery": delivery,
    }

    try:
        response = client.admit_prompt_response(
            args.session_id,
            payload,
            capabilities["route_availability"]["v2_prompt"]["path"],
        )
    except OpenCodeApiError as error:
        if _is_idempotent_admission_replay(error, message_id):
            if args.raw:
                _write_raw(error.body or "")
                return 0
            admission = _admission_record(args.session_id, delivery, message_id, error.data)
            if args.json:
                print(json.dumps(admission, sort_keys=True))
            else:
                print(_format_admission_compact(admission))
            return 0
        if error.status in {400, 404, 405, 415, 422}:
            print(
                f"opencode-session: unsupported v2 prompt behavior; {_api_error_detail(error)}; "
                "legacy run/reply fallback is not used",
                file=sys.stderr,
            )
            return EX_UNSUPPORTED
        print(
            f"opencode-session: prompt admission failed; {error}; legacy run/reply fallback is not used",
            file=sys.stderr,
        )
        return EX_UNAVAILABLE

    if args.raw:
        _write_raw(response.body)
        return 0

    admission = _admission_record(args.session_id, delivery, message_id, response.data)
    if args.json:
        print(json.dumps(admission, sort_keys=True))
    else:
        print(_format_admission_compact(admission))
    return 0


def _write_raw(body):
    sys.stdout.write(body)


def _format_session_compact(session):
    fields = [
        ("id", _session_value(session, "id", "sessionID", "sessionId")),
        ("title", _session_value(session, "title")),
        ("dir", _session_value(session, "directory", "cwd")),
        ("agent", _session_value(session, "agent")),
        ("model", _session_value(session, "model")),
        ("cost", _session_value(session, "cost")),
        ("tokens", _session_tokens(session)),
        ("created", _session_value(session, "createdAt", "created_at")),
        ("updated", _session_value(session, "updatedAt", "updated_at")),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _format_admission_compact(admission):
    fields = [
        ("state", admission["state"]),
        ("session", admission["session_id"]),
        ("message", admission["message_id"]),
        ("delivery", admission["delivery"]),
        ("admitted", admission["admitted_sequence"]),
        ("promoted", admission["promoted_sequence"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _admission_record(session_id, delivery, message_id, data):
    if not isinstance(data, dict):
        data = {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    return {
        "session_id": _first_present(data, "sessionID", "sessionId", "session_id")
        or _first_present(info, "sessionID", "sessionId", "session_id")
        or session_id,
        "message_id": _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
        or message_id,
        "delivery": _first_present(data, "delivery", "deliveryMode", "mode") or delivery,
        "state": _first_present(data, "state", "status", "phase") or "admitted",
        "admitted_sequence": _first_present(data, "admittedSequence", "admitted_sequence", "sequence"),
        "promoted_sequence": _first_present(data, "promotedSequence", "promoted_sequence"),
    }


def _is_idempotent_admission_replay(error, message_id):
    if error.status != 409 or not isinstance(error.data, dict):
        return False
    data = error.data
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    response_message_id = (
        _first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or _first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
    )
    if response_message_id != message_id:
        return False
    state = _first_present(data, "state", "status", "phase")
    idempotency = _first_present(data, "idempotency", "idempotencyStatus")
    return (
        data.get("duplicate") is True
        or data.get("idempotent") is True
        or idempotency in {"duplicate", "replayed", "existing"}
        or state in {"admitted", "promoted", "running", "completed", "failed"}
    )


def _api_error_detail(error):
    if isinstance(error.data, dict):
        for name in ("error", "message", "detail"):
            value = error.data.get(name)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = _first_present(value, "message", "detail", "error")
                if isinstance(nested, str):
                    return nested
    return str(error)


def _collection_sessions(collection):
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        for name in ("sessions", "data"):
            sessions = collection.get(name)
            if isinstance(sessions, list):
                return sessions
    return []


def _filter_sessions(sessions, *, directory=None, agent=None, model=None):
    filtered = []
    for session in sessions:
        if directory is not None and _session_value(session, "directory", "cwd") != directory:
            continue
        if agent is not None and _session_value(session, "agent") != agent:
            continue
        if model is not None and _session_value(session, "model") != model:
            continue
        filtered.append(session)
    return filtered


def _session_value(session, *names):
    for name in names:
        value = session.get(name)
        if value is not None:
            return value
    return None


def _first_present(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _session_tokens(session):
    tokens = session.get("tokens")
    if isinstance(tokens, dict):
        if tokens.get("total") is not None:
            return tokens["total"]
        return sum(value for value in tokens.values() if isinstance(value, int))
    return tokens


def _compact_value(value):
    if value is None or value == "":
        return "-"
    text = str(value)
    if any(character.isspace() for character in text):
        return json.dumps(text)
    return text
