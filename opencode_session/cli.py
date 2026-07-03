import argparse
import json
import os
import sys
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import (
    detect_capabilities,
    format_compact,
    legacy_run_reply_supported,
    unsupported_reasons,
)


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

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("prompt", nargs="*", help="prompt text; stdin is used when omitted")
    run_parser.add_argument("--session", help="existing session ID to run in")
    run_parser.add_argument("--directory", help="target directory when creating a disposable session")
    run_parser.add_argument("--agent", help="agent name for a disposable session")
    run_parser.add_argument("--model", help="model name for a disposable session")
    _add_server_argument(run_parser)
    run_parser.add_argument("--json", action="store_true", help="print normalized JSON result")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 64

    client = OpenCodeApiClient(args.server)
    if args.command == "run":
        prompt = _read_prompt(args.prompt)
        session_id = args.session
        created_session_id = None
        try:
            if not legacy_run_reply_supported(client.require_openapi_doc()):
                print(
                    "opencode-session: unsupported route behavior: missing legacy POST "
                    "/session/{sessionID}/run + POST /session/{sessionID}/reply; "
                    "v2 prompt admission is not execution",
                    file=sys.stderr,
                )
                return EX_UNSUPPORTED
            if session_id is None:
                directory = str(Path(args.directory or ".").resolve())
                create_response = client.create_session_response(
                    directory,
                    agent=args.agent,
                    model=args.model,
                )
                session_id = _session_value(create_response.data, "id", "sessionID", "sessionId")
                created_session_id = session_id
            run_response = client.run_session_response(session_id, prompt)
            provider_error = _provider_failure(run_response.data)
            if provider_error:
                cleanup_error = _delete_disposable_session(client, created_session_id)
                if cleanup_error:
                    _print_cleanup_error(cleanup_error)
                print(f"opencode-session: provider failure: {provider_error}", file=sys.stderr)
                return EX_UNAVAILABLE
            reply_response = client.reply_session_response(session_id)
            provider_error = _provider_failure(reply_response.data)
            if provider_error:
                cleanup_error = _delete_disposable_session(client, created_session_id)
                if cleanup_error:
                    _print_cleanup_error(cleanup_error)
                print(f"opencode-session: provider failure: {provider_error}", file=sys.stderr)
                return EX_UNAVAILABLE
        except OpenCodeApiError as error:
            cleanup_error = _delete_disposable_session(client, created_session_id)
            if cleanup_error:
                _print_cleanup_error(cleanup_error)
            if session_id is not None and _is_session_not_found_error(error):
                print(f"opencode-session: session not found: {session_id}", file=sys.stderr)
            else:
                print(f"opencode-session: api failure: {error}", file=sys.stderr)
            return EX_UNAVAILABLE
        cleanup_error = _delete_disposable_session(client, created_session_id)
        if cleanup_error:
            _print_cleanup_error(cleanup_error)
            return EX_UNAVAILABLE
        result = _run_result(session_id, run_response.data, reply_response.data)
        if args.json:
            print(json.dumps(result, sort_keys=True))
            return 0
        print(_format_run_compact(result))
        return 0

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


def _write_raw(body):
    sys.stdout.write(body)


def _read_prompt(prompt_words):
    if prompt_words:
        return " ".join(prompt_words)
    prompt = sys.stdin.read()
    if prompt.endswith("\n"):
        prompt = prompt[:-1]
    if prompt.endswith("\r"):
        prompt = prompt[:-1]
    return prompt


def _run_result(session_id, run_message, reply_message):
    return {
        "session_id": session_id,
        "message_ids": {
            "user": _message_value(run_message, "id", "messageID", "messageId"),
            "assistant": _message_value(reply_message, "id", "messageID", "messageId"),
        },
        "status": _message_value(reply_message, "status") or "completed",
        "cost": _message_value(reply_message, "cost"),
        "tokens": _message_tokens(reply_message),
        "text": _message_text(reply_message),
    }


def _format_run_compact(result):
    fields = [
        ("session", result["session_id"]),
        ("user", result["message_ids"]["user"]),
        ("assistant", result["message_ids"]["assistant"]),
        ("status", result["status"]),
        ("cost", result["cost"]),
        ("tokens", _tokens_total(result["tokens"])),
        ("text", result["text"]),
    ]
    return " ".join(f"{key}={_compact_value(value)}" for key, value in fields)


def _message_value(message, *names):
    for name in names:
        value = message.get(name)
        if value is not None:
            return value
    info = message.get("info")
    if isinstance(info, dict):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return None


def _message_tokens(message):
    tokens = _message_value(message, "tokens", "usage")
    return tokens


def _tokens_total(tokens):
    if isinstance(tokens, dict):
        if tokens.get("total") is not None:
            return tokens["total"]
        return sum(value for value in tokens.values() if isinstance(value, int))
    return tokens


def _message_text(message):
    text = _message_value(message, "text", "content")
    if text is not None:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _print_cleanup_error(error):
    print(f"opencode-session: api failure: disposable session cleanup failed: {error}", file=sys.stderr)


def _is_session_not_found_error(error):
    if error.status != 404:
        return False
    method = str(getattr(error, "method", "") or "").upper()
    path = str(getattr(error, "path", "") or "").split("?", 1)[0]
    parts = path.split("/")
    if method == "POST" and len(parts) == 4 and parts[1] == "session":
        return bool(parts[2]) and parts[3] in {"run", "reply"}
    return method in {"GET", "DELETE"} and len(parts) == 4 and parts[1:3] == ["api", "session"] and bool(parts[3])


def _delete_disposable_session(client, session_id):
    if session_id is None:
        return None
    try:
        client.delete_session(session_id)
    except OpenCodeApiError as error:
        return error
    return None


def _provider_failure(message):
    status = str(_message_value(message, "status") or "").lower()
    if status not in {"failed", "error", "errored"}:
        return None
    error = _message_value(message, "error", "reason", "message")
    if isinstance(error, dict):
        error = error.get("message") or json.dumps(error, sort_keys=True)
    return error or status


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
