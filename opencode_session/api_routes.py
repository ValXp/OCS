from urllib.parse import quote

from opencode_session.api_transport import OpenCodeApiError


DEFAULT_ROUTE_PLAN = {
    "session_collection": "/api/session",
    "session_item": "/api/session/{sessionID}",
    "blocking_message": "/session/{sessionID}/message",
    "legacy_run": "/session/{sessionID}/run",
    "legacy_reply": "/session/{sessionID}/reply",
}


class OpenCodeRoutePlanner:
    def __init__(self):
        self.route_plan = None

    def configure(self, route_plan):
        self.route_plan = {**DEFAULT_ROUTE_PLAN, **(route_plan or {})}
        return self

    def path(self, name, *, session_id=None):
        route_plan = self.require_route_plan()
        path = route_plan.get(name) or DEFAULT_ROUTE_PLAN[name]
        if session_id is not None:
            path = session_prompt_path(path, session_id)
        return path.lstrip("/")

    def require_route_plan(self):
        if self.route_plan is None:
            raise OpenCodeApiError(
                "client route plan is not configured; discover capabilities and configure routes before session calls",
                data={"kind": "route_plan_required"},
            )
        return self.route_plan


def session_prompt_path(prompt_path, session_id):
    path = prompt_path.lstrip("/")
    quoted_session_id = quote(session_id, safe="")
    for placeholder in ("{sessionID}", ":sessionID", "{id}", ":id"):
        path = path.replace(placeholder, quoted_session_id)
    return path
