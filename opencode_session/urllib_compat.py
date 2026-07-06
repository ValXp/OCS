def set_response_socket_timeout(response, timeout):
    """Best-effort read timeout adjustment for urllib response objects."""
    socket = _response_socket(response)
    if socket is None or not hasattr(socket, "settimeout"):
        return False
    try:
        socket.settimeout(timeout)
    except (AttributeError, OSError, ValueError):
        return False
    return True


def _response_socket(response):
    socket = getattr(response, "sock", None)
    if socket is not None:
        return socket
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    return getattr(raw, "_sock", None)
