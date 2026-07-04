def first_present(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def first_present_in(sources, *names):
    for source in sources:
        value = first_present(source, *names)
        if value is not None:
            return value
    return None


def session_record(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        return session["data"]
    return session if isinstance(session, dict) else {}


def session_value(session, *names):
    session = session_record(session)
    value = first_present(session, *names)
    if value is not None:
        return value
    info = session.get("info")
    value = first_present(info, *names)
    if value is not None:
        return value
    location = session.get("location")
    if isinstance(location, dict):
        for name in names:
            if name in {"directory", "cwd"} and location.get("directory") is not None:
                return location.get("directory")
    time = session.get("time")
    if isinstance(time, dict):
        for name in names:
            if name in {"createdAt", "created_at"} and time.get("created") is not None:
                return time.get("created")
            if name in {"updatedAt", "updated_at"} and time.get("updated") is not None:
                return time.get("updated")
    return None


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


def message_value(message, *names):
    message = message if isinstance(message, dict) else {}
    value = first_present(message, *names)
    if value is not None:
        return value
    return first_present(message.get("info"), *names)


def message_tokens(message):
    return message_value(message, "tokens", "usage")


def normalized_tokens(tokens):
    if isinstance(tokens, dict):
        normalized = dict(tokens)
        if normalized.get("total") is None:
            values = [value for value in normalized.values() if isinstance(value, int)]
            if values:
                normalized["total"] = sum(values)
        return normalized
    return tokens


def tokens_total(tokens):
    tokens = normalized_tokens(tokens)
    if isinstance(tokens, dict):
        return tokens.get("total")
    return tokens


def message_text(message):
    message = message if isinstance(message, dict) else {}
    text = message_value(message, "text", "content")
    if text is not None:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "1", "accepted", "aborted", "ok", "success"}:
            return True
        if lowered in {"false", "no", "0", "rejected", "failed", "error"}:
            return False
    return None
