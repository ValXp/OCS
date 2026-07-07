from datetime import datetime, timezone
from typing import Any, List, Tuple


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def string_list(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def append_unique_string(values: List[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)
