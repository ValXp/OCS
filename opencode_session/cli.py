import argparse
import json
import os
import sys
from pathlib import Path

from opencode_session.api_client import OpenCodeApiClient, OpenCodeApiError
from opencode_session.capabilities import detect_capabilities, format_compact, unsupported_reasons
from opencode_session.run_store import RunStore, RunStoreError, default_store_root, format_run_compact


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
EX_UNAVAILABLE = 69
EX_UNSUPPORTED = 70
EX_DATAERR = 65
EX_NOINPUT = 66


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
    run_parser.add_argument("--store", default=default_store_root(), help="local orchestration run store directory")
    run_subparsers = run_parser.add_subparsers(dest="run_command")
    run_subparsers.required = True

    run_init_parser = run_subparsers.add_parser("init")
    run_init_parser.add_argument("name", help="local run name")
    run_init_parser.add_argument("--directory", default=".", help="target directory for the run")
    _add_server_argument(run_init_parser)

    run_status_parser = run_subparsers.add_parser("status")
    run_status_parser.add_argument("name", help="local run name")
    run_status_parser.add_argument("--json", action="store_true", help="print complete run JSON data")

    run_worker_parser = run_subparsers.add_parser("worker")
    run_worker_parser.add_argument("name", help="local run name")
    run_worker_parser.add_argument("worker_id", help="worker record ID")
    run_worker_parser.add_argument("--role", help="worker role")
    run_worker_parser.add_argument("--session", dest="session_id", help="OpenCode session ID reference")
    run_worker_parser.add_argument("--agent", help="agent metadata")
    run_worker_parser.add_argument("--model", help="model metadata")
    run_worker_parser.add_argument("--depends-on", dest="dependencies", action="append", help="worker dependency ID")
    run_worker_parser.add_argument("--prompt-id", dest="prompt_ids", action="append", help="prompt admission ID")
    run_worker_parser.add_argument("--status", help="worker status")
    run_worker_parser.add_argument("--retry-count", type=int, help="worker retry count")
    run_worker_parser.add_argument("--timeout-seconds", type=int, help="worker timeout in seconds")
    run_worker_parser.add_argument("--blocker", dest="blockers", action="append", help="blocker reference")
    run_worker_parser.add_argument("--output-ref", dest="output_refs", action="append", help="output reference")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 64

    if args.command == "run":
        return _handle_run_command(args)

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


def _handle_run_command(args):
    store = RunStore(args.store)
    try:
        if args.run_command == "init":
            run = store.create_run(args.name, directory=args.directory, server_url=args.server)
            print(format_run_compact(run))
            return 0
        if args.run_command == "status":
            run = store.load_run(args.name)
            if args.json:
                print(json.dumps(run, sort_keys=True))
                return 0
            print(format_run_compact(run))
            return 0
        if args.run_command == "worker":
            run = store.upsert_worker(
                args.name,
                args.worker_id,
                role=args.role,
                session_id=args.session_id,
                agent=args.agent,
                model=args.model,
                dependencies=args.dependencies,
                prompt_ids=args.prompt_ids,
                status=args.status,
                retry_count=args.retry_count,
                timeout_seconds=args.timeout_seconds,
                blockers=args.blockers,
                output_refs=args.output_refs,
            )
            print(format_run_compact(run))
            return 0
    except RunStoreError as error:
        print(f"opencode-session: {error}", file=sys.stderr)
        if error.kind == "missing":
            return EX_NOINPUT
        return EX_DATAERR
    return 64


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
