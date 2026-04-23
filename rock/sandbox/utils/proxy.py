BLOCKED_WS_HEADER_NAMES = {
    "host",
    "connection",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
    "transfer-encoding",
    "te",
    "trailer",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "content-length",
}


def build_upstream_ws_headers(client_websocket):
    origin = client_websocket.headers.get("origin") or client_websocket.headers.get("Origin")
    additional_headers = []

    for key, value in client_websocket.headers.items():
        lower_key = key.lower()
        if lower_key == "origin":
            continue
        if lower_key in BLOCKED_WS_HEADER_NAMES:
            continue
        additional_headers.append((key, value))

    return origin, additional_headers or None
