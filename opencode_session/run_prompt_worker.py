from opencode_session.cli_policy import server_default
from opencode_session.run_store import RunStoreError


def ensure_prompt_worker(store, request):
    try:
        store.load_run(request.name)
    except RunStoreError as error:
        if error.kind != "missing":
            raise
        store.create_run(
            request.name,
            directory=request.directory or ".",
            server_url=request.server_url or getattr(request, "default_server_url", None) or server_default(),
        )
    store.upsert_worker(
        request.name,
        request.worker_id,
        role=request.role,
        prompt=request.prompt,
        status="queued",
        session_id=request.session_id,
        agent=request.agent,
        model=request.model,
    )
