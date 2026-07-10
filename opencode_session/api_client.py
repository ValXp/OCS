from opencode_session.api_domain import OpenCodeDomainClient, with_session_payload as _with_session_payload
from opencode_session.api_routes import DEFAULT_ROUTE_PLAN, OpenCodeRoutePlanner, session_prompt_path as _session_prompt_path
from opencode_session.api_transport import (
    OpenCodeApiError,
    OpenCodeApiResponse,
    OpenCodeApiTimeoutError,
    OpenCodeApiTransport,
)


class OpenCodeApiClient(OpenCodeDomainClient):
    def __init__(self, base_url, *, timeout=3):
        super().__init__(OpenCodeApiTransport(base_url, timeout=timeout), OpenCodeRoutePlanner())

    @property
    def base_url(self):
        return self._transport.base_url

    @base_url.setter
    def base_url(self, base_url):
        self._transport.base_url = base_url

    @property
    def timeout(self):
        return self._transport.timeout

    @timeout.setter
    def timeout(self, timeout):
        self._transport.timeout = timeout

    def stream_events(self, path, *, on_open=None, deadline=None, stop_event=None):
        yield from self._transport.stream_events(path, on_open=on_open, deadline=deadline, stop_event=stop_event)
