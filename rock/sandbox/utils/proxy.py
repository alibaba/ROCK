FORWARDED_WS_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "x-request-id",
    "traceparent",
    "tracestate",
    "eagleeye-traceid",
    "eagleeye-rpcid",
    "eagleeye-userdata",
}


def build_upstream_ws_headers(client_websocket):
    origin = client_websocket.headers.get("origin") or client_websocket.headers.get("Origin")
    additional_headers = []

    for key, value in client_websocket.headers.items():
        lower_key = key.lower()
        if lower_key == "origin":
            continue
        if lower_key in FORWARDED_WS_HEADER_NAMES:
            additional_headers.append((key, value))

    return origin, additional_headers or None
