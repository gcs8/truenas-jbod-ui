"""Read-only synthetic DDP producer, confined to the UI container's loopback."""
from __future__ import annotations

import argparse
import json
import time

from websockets.sync.client import connect
from websockets.sync.server import serve


CALLS: list[str] = []
RESPONSES = {
    "enclosure.query": [{"id": "matrix-shelf", "name": "Synthetic shelf", "elements": [
        {"slot": 1, "status": "Not installed"},
    ]}],
    "disk.query": [],
    "pool.query": [],
    "disk.temperatures": {},
    "smart.test.results": [],
}


def handle(connection):
    authenticated = False
    for raw in connection:
        request = json.loads(raw)
        if request.get("msg") == "connect":
            response = {"msg": "connected", "session": "compose-runtime"}
        elif request.get("msg") == "ping":
            response = {"msg": "pong"}
        elif request.get("msg") == "method":
            method = request.get("method")
            response = {"msg": "result", "id": request.get("id")}
            if method == "auth.login_with_api_key":
                authenticated = request.get("params") == ["synthetic-compose-source-key"]
                response["result"] = authenticated
            elif authenticated and method in RESPONSES:
                # Retain only a bounded native-test observation, never payloads.
                CALLS.append(method)
                del CALLS[:-256]
                response["result"] = RESPONSES[method]
            else:
                response["error"] = {"error": 32601, "reason": "Synthetic method refused"}
        else:
            connection.close()
            return
        connection.send(json.dumps(response))


def check(port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with connect(f"ws://127.0.0.1:{port}/websocket", open_timeout=1, close_timeout=1) as connection:
                connection.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
                if json.loads(connection.recv(timeout=1)) == {"msg": "connected", "session": "compose-runtime"}:
                    return
        except (OSError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError("Synthetic mapping source did not become ready.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19443)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check(args.port)
    else:
        with serve(handle, "127.0.0.1", args.port, max_size=65536) as server:
            server.serve_forever()
