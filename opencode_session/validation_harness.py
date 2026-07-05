import json

from opencode_session.api_client import OpenCodeApiError
from opencode_session.capabilities import detect_capabilities
from opencode_session.disposable_session_lifecycle import cleanup_disposable_sessions


class DisposableValidationError(Exception):
    def __init__(self, message, *, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


class DisposableValidationHarness:
    def __init__(self, client, result, *, default_exit_code, cleanup_failure_message):
        self.client = client
        self.result = result
        self.default_exit_code = default_exit_code
        self.cleanup_failure_message = cleanup_failure_message
        self.session_ids = []
        self.failure = None
        self.exit_code = default_exit_code
        self.result.setdefault("checks", {})

    def detect_capabilities(self):
        capabilities = detect_capabilities(self.client)
        self.result["capabilities"] = capabilities
        self.result["health"] = capabilities["health"]
        self.result["version"] = capabilities["version"]
        self.result["checks"]["capabilities"] = {
            "status": "done",
            "health": capabilities["health"],
            "version": capabilities["version"],
        }
        return capabilities

    def track_session(self, session_id):
        if session_id is not None:
            self.session_ids.append(session_id)
        return session_id

    def run(
        self,
        validation_body,
        *,
        failure_types,
        json_output,
        compact_formatter,
        failure_prefix,
        print_error,
        cleanup_summary_formatter,
    ):
        try:
            validation_body(self)
        except failure_types as error:
            self.record_failure(error)
        except OpenCodeApiError as error:
            self.record_failure(error)
        else:
            self.result["status"] = "done"
            self.result["ok"] = True
            self.exit_code = 0

        cleanup = cleanup_disposable_sessions(self.client, self.session_ids).record
        self.result["cleanup"] = cleanup
        self.result["checks"]["cleanup"] = cleanup
        if cleanup["status"] != "done" and self.failure is None:
            self.record_failure(
                DisposableValidationError(
                    self.cleanup_failure_message,
                    exit_code=self.default_exit_code,
                )
            )

        if self.failure is not None:
            print_error(f"{failure_prefix}: {self.failure}; {cleanup_summary_formatter(cleanup)}")
            return self.exit_code

        if json_output:
            print(json.dumps(self.result, sort_keys=True))
        else:
            print(compact_formatter(self.result))
        return 0

    def record_failure(self, error):
        self.failure = error
        self.exit_code = getattr(error, "exit_code", self.default_exit_code)
        self.result["status"] = "failed"
        self.result["ok"] = False
        self.result["error"] = str(error)
