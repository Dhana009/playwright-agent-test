from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from threading import Thread
from typing import Iterator

from .live import TargetInfo, live_target
from .targets import choose_target, offline_target


@dataclass
class FixtureServer:
    host: str
    port: int
    thread: Thread
    server: ThreadingHTTPServer

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def start_server(host: str = "127.0.0.1", port: int = 8787) -> FixtureServer:
    from .serve import build_server

    server = build_server(host=host, port=port)
    actual_port = int(server.server_address[1])
    thread = Thread(target=server.serve_forever, name="fixture-http-server", daemon=True)
    thread.start()
    return FixtureServer(host=host, port=actual_port, thread=thread, server=server)


def stop_server(handle: FixtureServer) -> None:
    handle.server.shutdown()
    handle.server.server_close()
    handle.thread.join(timeout=2)


@contextmanager
def running_server(host: str = "127.0.0.1", port: int = 8787) -> Iterator[FixtureServer]:
    handle = start_server(host=host, port=port)
    try:
        yield handle
    finally:
        stop_server(handle)


__all__ = [
    "FixtureServer",
    "TargetInfo",
    "choose_target",
    "live_target",
    "offline_target",
    "running_server",
    "start_server",
    "stop_server",
]
