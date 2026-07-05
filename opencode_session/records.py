from opencode_session.schema_normalization import (
    bool_value,
    first_present,
    first_present_in,
    message_text,
    message_tokens,
    message_value,
    normalized_tokens,
    session_record,
    session_value,
    tokens_total,
)


def collection_records(collection, *names):
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        for name in names:
            records = collection.get(name)
            if isinstance(records, list):
                return records
    return []


def collection_sessions(collection):
    return collection_records(collection, "sessions", "children", "data")
