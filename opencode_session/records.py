def first_present(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def session_record(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        return session["data"]
    return session if isinstance(session, dict) else {}


def session_value(session, *names):
    session = session_record(session)
    for name in names:
        value = session.get(name)
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
