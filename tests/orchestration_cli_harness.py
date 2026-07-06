import time
from collections import deque


def configure_single_worker_server(
    server,
    *,
    events=None,
    run_payload=None,
    reply_payload=None,
    message_payload=None,
    session_ids=None,
    modern_message=False,
):
    session_ids = list(session_ids or ["ses_new"])
    run_payload = run_payload or {"id": "msg_user_1", "status": "submitted"}
    reply_payload = reply_payload or {
        "id": "msg_assistant_1",
        "status": "completed",
        "cost": 0.015,
        "tokens": {"input": 12, "output": 8, "total": 20},
        "text": "Worker finished.",
    }
    message_payload = message_payload or {
        "info": {
            "id": "msg_assistant_modern_1",
            "sessionID": "ses_new",
            "role": "assistant",
            "cost": 0.02,
            "tokens": {"input": 3, "output": 2, "total": 5},
        },
        "parts": [{"type": "text", "text": "Modern worker finished."}],
    }
    paths = {
        "/api/session": {"get": {}, "post": {}},
        "/api/event": {"get": {}},
    }
    if modern_message:
        paths["/session/{sessionID}/message"] = {"post": {}}
    else:
        paths["/session/{sessionID}/run"] = {"post": {}}
        paths["/session/{sessionID}/reply"] = {"post": {}}

    _register_core_routes(server, paths)
    server.sse("/api/event", events or [])
    server.json(
        "POST",
        "/api/session",
        lambda request: {"id": "ses_new", "directory": payload_directory(request.payload)},
    )
    for session_id in session_ids:
        if modern_message:
            server.json("POST", f"/session/{session_id}/message", message_payload)
        else:
            server.json("POST", f"/session/{session_id}/run", run_payload)
            server.json("POST", f"/session/{session_id}/reply", reply_payload)
        server.json("DELETE", f"/api/session/{session_id}", {"id": session_id, "deleted": True})
        server.json("GET", f"/api/session/{session_id}", {"error": "not found"}, status=404)
    return server


def configure_multi_worker_server(
    server,
    *,
    session_ids=None,
    run_payloads=None,
    reply_payloads=None,
):
    session_ids = list(session_ids or ["ses_docs", "ses_plan"])
    run_payloads = run_payloads if run_payloads is not None else {
        "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
        "ses_docs": {"id": "msg_docs_user", "status": "submitted"},
    }
    reply_payloads = reply_payloads if reply_payloads is not None else {
        "ses_plan": {
            "id": "msg_plan_assistant",
            "status": "completed",
            "cost": 0.01,
            "tokens": {"input": 8, "output": 4, "total": 12},
            "text": "Plan ready.",
        },
        "ses_docs": {
            "id": "msg_docs_assistant",
            "status": "completed",
            "cost": 0.02,
            "tokens": {"input": 10, "output": 7, "total": 17},
            "text": "Docs ready.",
        },
    }
    queued_session_ids = deque(session_ids)
    _register_core_routes(
        server,
        {
            "/api/session": {"get": {}, "post": {}},
            "/session/{sessionID}/run": {"post": {}},
            "/session/{sessionID}/reply": {"post": {}},
        },
    )
    server.json(
        "POST",
        "/api/session",
        lambda request: {
            "id": queued_session_ids.popleft(),
            "directory": payload_directory(request.payload),
        },
    )
    for session_id, run_payload in run_payloads.items():
        server.json("POST", f"/session/{session_id}/run", run_payload)
    for session_id, reply_payload in reply_payloads.items():
        server.json("POST", f"/session/{session_id}/reply", reply_payload)
    for session_id in sorted(set(session_ids) | set(run_payloads) | set(reply_payloads)):
        server.json("DELETE", f"/api/session/{session_id}", {"id": session_id, "deleted": True})
        server.json("GET", f"/api/session/{session_id}", {"error": "not found"}, status=404)
    return server


def configure_retry_server(
    server,
    *,
    run_payloads,
    reply_payloads=None,
    session_id="ses_retry",
    session_ids=None,
):
    session_ids = list(session_ids or [session_id])
    reply_payloads = reply_payloads or [
        {
            "id": "msg_assistant_1",
            "status": "completed",
            "cost": 0.015,
            "tokens": {"total": 20},
            "text": "Worker finished after retry.",
        }
    ]
    _register_core_routes(
        server,
        {
            "/api/session": {"get": {}, "post": {}},
            "/session/{sessionID}/run": {"post": {}},
            "/session/{sessionID}/reply": {"post": {}},
        },
    )
    queued_session_ids = deque(session_ids)
    server.json(
        "POST",
        "/api/session",
        lambda request: {"id": queued_session_ids.popleft(), "directory": payload_directory(request.payload)},
    )
    for session_id, session_run_payloads in _retry_payloads_by_session(run_payloads, session_ids).items():
        _register_json_sequence(server, "POST", f"/session/{session_id}/run", session_run_payloads)
    for session_id, session_reply_payloads in _retry_payloads_by_session(reply_payloads, session_ids).items():
        _register_json_sequence(server, "POST", f"/session/{session_id}/reply", session_reply_payloads)
    for session_id in session_ids:
        server.json("DELETE", f"/api/session/{session_id}", {"id": session_id, "deleted": True})
        server.json("GET", f"/api/session/{session_id}", {"error": "not found"}, status=404)
    return server


def configure_worker_control_server(
    server,
    *,
    prompt_response=None,
    prompt_status=200,
    abort_response=None,
):
    _register_core_routes(
        server,
        {
            "/api/session": {"get": {}, "post": {}},
            "/api/session/{sessionID}/prompt": {"post": {}},
            "/session/{sessionID}/abort": {"post": {}},
        },
    )
    server.json("POST", "/api/session/ses_plan/prompt", prompt_response or {}, status=prompt_status)
    server.json("POST", "/session/ses_plan/abort", abort_response or {})
    return server


def request_paths(requests):
    return [(method, path) for method, path, _payload in requests]


def payloads_for(requests, method, path):
    return [
        payload
        for request_method, request_path, payload in requests
        if request_method == method and request_path == path
    ]


def assert_worker_session_create_payload(test_case, payload, *, directory, worker_id, run_name="demo", **expected_fields):
    expected_payload = {"location": {"directory": directory}, **expected_fields}
    payload_without_metadata = {key: value for key, value in payload.items() if key != "metadata"}
    test_case.assertEqual(payload_without_metadata, expected_payload)
    metadata = payload.get("metadata")
    test_case.assertIsInstance(metadata, dict)
    test_case.assertEqual(metadata.get("ocs.remote_mutation_kind"), "worker_session_create")
    test_case.assertTrue(metadata.get("ocs.remote_mutation_id", "").startswith("worker_session_create_"))
    test_case.assertEqual(metadata.get("ocs.worker_id"), worker_id)
    test_case.assertEqual(metadata.get("ocs.cleanup_requested"), "false")
    test_case.assertEqual(metadata.get("ocs.run_name"), run_name)


def payload_directory(payload):
    payload = payload or {}
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return location.get("directory") or payload.get("directory")


def _retry_payloads_by_session(payloads, session_ids):
    if isinstance(payloads, dict):
        return payloads
    if len(session_ids) == 1:
        return {session_ids[0]: payloads}
    if len(payloads) == len(session_ids):
        return {session_id: [payload] for session_id, payload in zip(session_ids, payloads)}
    return {session_id: payloads for session_id in session_ids}


def _register_core_routes(server, paths):
    server.json("GET", "/global/health", {"status": "ok", "version": "2.0.0"})
    server.json("GET", "/doc", {"openapi": "3.1.0", "paths": paths})


def _register_json_sequence(server, method, path, payloads):
    queued_payloads = deque(payloads)

    def responder(handler, _request):
        status, payload = _normalize_sequence_payload(queued_payloads.popleft())
        handler._write_json(payload, status=status)

    server.route(method, path, responder)


def _normalize_sequence_payload(payload):
    if isinstance(payload, tuple) and payload and payload[0] == "sleep":
        _marker, delay, payload = payload
        time.sleep(delay)
    if isinstance(payload, tuple):
        return payload
    return 200, payload
