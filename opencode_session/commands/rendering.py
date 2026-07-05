import json

from opencode_session.formatting import write_raw


def render_command_result(args, data=None, *, raw_body=None, compact=None):
    if getattr(args, "raw", False) and raw_body is not None:
        write_raw(raw_body)
        return 0
    if getattr(args, "json", False):
        print(json.dumps(data, sort_keys=True))
        return 0
    if compact is not None:
        print(compact(data) if callable(compact) else compact)
    return 0
