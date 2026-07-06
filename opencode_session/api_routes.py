from opencode_session.api_profile import DEFAULT_ROUTE_PLAN, OpenCodeServerProfile, render_route_path
from opencode_session.api_transport import OpenCodeApiError


class OpenCodeRoutePlanner:
    def __init__(self):
        self.route_plan = None
        self.server_profile = None

    def configure(self, route_plan):
        self.server_profile = OpenCodeServerProfile.from_route_plan(route_plan)
        self.route_plan = self.server_profile.route_plan
        return self

    def path(self, name, *, session_id=None, request_id=None, allow_default=False):
        route_plan = DEFAULT_ROUTE_PLAN if allow_default and self.route_plan is None else self.require_route_plan()
        path = route_plan.get(name) or DEFAULT_ROUTE_PLAN[name]
        if session_id is not None or request_id is not None:
            path = render_route_path(path, session_id=session_id, request_id=request_id)
        return path.lstrip("/")

    def require_route_plan(self):
        if self.route_plan is None:
            raise OpenCodeApiError(
                "client route plan is not configured; discover capabilities and configure routes before session calls",
                data={"kind": "route_plan_required"},
            )
        return self.route_plan


def session_prompt_path(prompt_path, session_id):
    return render_route_path(prompt_path, session_id=session_id).lstrip("/")
