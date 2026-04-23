from __future__ import annotations

import argparse
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse


class _FixtureState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.region_version = 1
        self.route_variant = "route-v1"
        self.modal_open = False
        self.stale_ref_version = 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "regionVersion": self.region_version,
                "routeVariant": self.route_variant,
                "modalOpen": self.modal_open,
                "staleRefVersion": self.stale_ref_version,
            }

    def mutate(self, kind: str) -> dict[str, object]:
        with self._lock:
            if kind == "region":
                self.region_version = 2 if self.region_version == 1 else 1
                changed = {"key": "regionVersion", "value": self.region_version}
            elif kind == "route":
                self.route_variant = "route-v2" if self.route_variant == "route-v1" else "route-v1"
                changed = {"key": "routeVariant", "value": self.route_variant}
            elif kind == "modal":
                self.modal_open = not self.modal_open
                changed = {"key": "modalOpen", "value": self.modal_open}
            elif kind == "stale-ref":
                self.stale_ref_version = 2 if self.stale_ref_version == 1 else 1
                changed = {"key": "staleRefVersion", "value": self.stale_ref_version}
            else:
                raise KeyError(kind)

            return {
                "mutation": kind,
                "changed": changed,
                "state": {
                    "regionVersion": self.region_version,
                    "routeVariant": self.route_variant,
                    "modalOpen": self.modal_open,
                    "staleRefVersion": self.stale_ref_version,
                },
            }


class FixtureHTTPRequestHandler(SimpleHTTPRequestHandler):
    state: _FixtureState

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/fixture-state":
            self._write_json(200, self.state.snapshot())
            return
        if path == "/mutate/region":
            self._write_json(200, self.state.mutate("region"))
            return
        if path == "/mutate/route":
            self._write_json(200, self.state.mutate("route"))
            return
        if path == "/mutate/modal":
            self._write_json(200, self.state.mutate("modal"))
            return
        if path == "/mutate/stale-ref":
            self._write_json(200, self.state.mutate("stale-ref"))
            return
        if path in {"", "/"}:
            self.path = "/login.html"
            super().do_GET()
            return
        super().do_GET()

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    root_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    app_root = Path(root_dir) if root_dir is not None else _default_app_root()
    state = _FixtureState()

    class _Handler(FixtureHTTPRequestHandler):
        pass

    _Handler.state = state
    handler = partial(_Handler, directory=str(app_root))
    return ThreadingHTTPServer((host, port), handler)


def serve_forever(
    host: str = "127.0.0.1",
    port: int = 8787,
    root_dir: str | Path | None = None,
) -> None:
    app_root = Path(root_dir) if root_dir is not None else _default_app_root()
    server = build_server(host=host, port=port, root_dir=app_root)
    try:
        print(f"Serving fixture app from {app_root} at http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping fixture server...")
    finally:
        server.server_close()


def _default_app_root() -> Path:
    return Path(__file__).resolve().parent / "app"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve local fixture pages for smoke tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Optional directory containing fixture HTML files.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    serve_forever(host=args.host, port=args.port, root_dir=args.root_dir)


if __name__ == "__main__":
    main()
