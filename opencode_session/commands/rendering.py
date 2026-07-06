from collections.abc import Mapping
import json
from dataclasses import dataclass

from opencode_session.formatting import write_raw


@dataclass(frozen=True)
class CommandResult:
    data: object = None
    raw_body: object = None
    compact: object = None
    exit_code: int = 0
    error: str = None
    warnings: tuple = ()


def render_command_result(args, result=None, *, raw_body=None, compact=None, exit_code=0, print_error=None):
    if isinstance(result, CommandResult):
        command_result = result
    else:
        command_result = CommandResult(result, raw_body=raw_body, compact=compact, exit_code=exit_code)

    if print_error is not None:
        for warning in command_result.warnings:
            print_error(warning)
    if command_result.error is not None:
        if print_error is not None:
            print_error(command_result.error)
        return command_result.exit_code

    data = command_result.data
    raw_body = command_result.raw_body
    compact = command_result.compact
    if getattr(args, "raw", False) and raw_body is not None:
        write_raw(raw_body)
        return command_result.exit_code
    if getattr(args, "json", False):
        print(json.dumps(_json_ready(data), sort_keys=True))
        return command_result.exit_code
    if compact is not None:
        print(compact(data) if callable(compact) else compact)
    return command_result.exit_code


def _json_ready(value):
    to_public_dict = getattr(value, "to_public_dict", None)
    if callable(to_public_dict):
        return _json_ready(to_public_dict())
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value
