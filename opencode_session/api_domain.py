from opencode_session.api_routes import session_prompt_path
from opencode_session.api_transport import OpenCodeApiError, OpenCodeApiResponse
from opencode_session.schema_session_adapter import normalize_session_payload


DEFAULT_PROFILE_ENDPOINTS = frozenset(
    {
        "session_abort",
        "session_fork",
        "session_children",
        "permissions",
        "permission_reply",
        "questions",
        "question_reply",
        "question_reject",
    }
)


class OpenCodeDomainClient:
    def __init__(self, transport, routes):
        self._transport = transport
        self._routes = routes

    @property
    def route_plan(self):
        return self._routes.route_plan

    @route_plan.setter
    def route_plan(self, route_plan):
        self._routes.configure(route_plan)

    @property
    def server_profile(self):
        return self._routes.server_profile

    def configure_route_plan(self, route_plan):
        self._routes.configure(route_plan)
        return self

    def configure_server_profile(self, profile):
        self._routes.configure(profile)
        return self

    def get_json(self, path, *, timeout=None, deadline=None):
        return self._transport.get_json(path, timeout=timeout, deadline=deadline)

    def get_response(self, path, *, timeout=None, deadline=None):
        return self._transport.get_response(path, timeout=timeout, deadline=deadline)

    def get_response_no_redirects(self, path, *, timeout=None, deadline=None):
        return self._transport.get_response_no_redirects(path, timeout=timeout, deadline=deadline)

    def post_json(self, path, payload, *, timeout=None, deadline=None):
        return self._transport.post_json(path, payload, timeout=timeout, deadline=deadline)

    def post_response(self, path, payload, *, timeout=None, deadline=None):
        return self._transport.post_response(path, payload, timeout=timeout, deadline=deadline)

    def delete_json(self, path, *, timeout=None, deadline=None):
        return self._transport.delete_json(path, timeout=timeout, deadline=deadline)

    def delete_response(self, path, *, timeout=None, deadline=None):
        return self._transport.delete_response(path, timeout=timeout, deadline=deadline)

    def get_health(self, *, deadline=None):
        errors = []
        for path in ("global/health", "api/health", "health"):
            try:
                return self.get_json(path, deadline=deadline)
            except OpenCodeApiError as error:
                errors.append(str(error))
        raise OpenCodeApiError("; ".join(errors))

    def get_openapi_doc(self, *, deadline=None):
        try:
            return self.get_json("doc", deadline=deadline)
        except OpenCodeApiError:
            return {"paths": {}}

    def require_openapi_doc(self, *, deadline=None):
        return self.get_json("doc", deadline=deadline)

    def create_session(self, directory, *, agent=None, model=None, title=None, metadata=None):
        return self.create_session_response(directory, agent=agent, model=model, title=title, metadata=metadata).data

    def create_session_response(self, directory, *, agent=None, model=None, title=None, metadata=None):
        payload = {"location": {"directory": directory}}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        response = self.post_response(self._route_path("session_collection"), payload)
        return with_session_payload(response, self.server_profile)

    def list_sessions(self):
        return self.list_sessions_response().data

    def list_sessions_response(self):
        response = self.get_response(self._route_path("session_collection"))
        return with_session_payload(response, self.server_profile)

    def get_session(self, session_id):
        return self.get_session_response(session_id).data

    def get_session_response(self, session_id):
        response = self.get_response(self._route_path("session_item", session_id=session_id))
        return with_session_payload(response, self.server_profile)

    def delete_session(self, session_id):
        return self.delete_session_response(session_id).data

    def delete_session_response(self, session_id):
        return self.delete_response(self._route_path("session_item", session_id=session_id))

    def abort_session_response(self, session_id):
        return self.post_response(self._route_path("session_abort", session_id=session_id), {})

    def fork_session_response(self, session_id, *, message_id=None):
        payload = {}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(self._route_path("session_fork", session_id=session_id), payload)

    def list_child_sessions_response(self, session_id):
        response = self.get_response(self._route_path("session_children", session_id=session_id))
        return with_session_payload(response, self.server_profile, endpoint="session_children")

    def run_session_response(self, session_id, message, *, timeout=None, deadline=None):
        return self.post_response(
            self._route_path("legacy_run", session_id=session_id),
            {"message": message},
            timeout=timeout,
            deadline=deadline,
        )

    def reply_session_response(self, session_id, *, timeout=None, deadline=None):
        return self.post_response(
            self._route_path("legacy_reply", session_id=session_id),
            {},
            timeout=timeout,
            deadline=deadline,
        )

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None, deadline=None):
        payload = {"parts": [{"type": "text", "text": message}]}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(
            self._route_path("blocking_message", session_id=session_id),
            payload,
            timeout=timeout,
            deadline=deadline,
        )

    def admit_prompt_response(self, session_id, payload, prompt_path):
        return self.post_response(session_prompt_path(prompt_path, session_id), payload)

    def wait_session_response(self, session_id, wait_path, *, deadline=None):
        return self.post_response(session_prompt_path(wait_path, session_id), {}, deadline=deadline)

    def list_permissions_response(self):
        return self.get_response(self._route_path("permissions"))

    def reply_permission_response(self, request_id, reply, *, message=None):
        payload = {"reply": reply}
        if message is not None:
            payload["message"] = message
        return self.post_response(self._route_path("permission_reply", request_id=request_id), payload)

    def list_questions_response(self):
        return self.get_response(self._route_path("questions"))

    def answer_question_response(self, request_id, answers):
        return self.post_response(self._route_path("question_reply", request_id=request_id), {"answers": answers})

    def reject_question_response(self, request_id):
        return self.post_response(self._route_path("question_reject", request_id=request_id), {})

    def list_projects_response(self):
        return self.get_response(self._route_path("project_collection"))

    def list_project_directories_response(self, project_id):
        return self.get_response(self._route_path("project_directories", project_id=project_id))

    def list_workspaces_response(self):
        return self.get_response(self._route_path("workspace_collection"))

    def delete_workspace_response(self, workspace_id):
        return self.delete_response(self._route_path("workspace_item", workspace_id=workspace_id))

    def refresh_project_copies_response(self, project_id):
        return self.post_response(self._route_path("project_copy_refresh", project_id=project_id), {})

    def _route_path(self, name, *, session_id=None, request_id=None, project_id=None, workspace_id=None):
        return self._routes.path(
            name,
            session_id=session_id,
            request_id=request_id,
            project_id=project_id,
            workspace_id=workspace_id,
            allow_default=name in DEFAULT_PROFILE_ENDPOINTS,
        )


def with_session_payload(response, route_plan=None, *, route_path=None, endpoint="session_collection"):
    if route_path is None and hasattr(route_plan, "normalize_session_payload"):
        data = route_plan.normalize_session_payload(response.data, endpoint=endpoint)
    else:
        data = normalize_session_payload(response.data, route_plan=route_plan, route_path=route_path)
    return OpenCodeApiResponse(data, response.body)
