"""Unix-socket harness that speaks the real CMUX JSONL protocol."""

from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path


def short_socket_path(test_path: Path, label: str) -> Path:
    """Return a deterministic AF_UNIX path below macOS's short path limit."""
    digest = sha256(f"{test_path}:{label}".encode()).hexdigest()[:16]
    return Path("/tmp") / f"yata-{digest}.sock"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        harness: CmuxSocketHarness = self.server.harness  # type: ignore[attr-defined]
        request = self._read_request()
        if request.get("method") == "auth.login":
            harness.auth_attempts.append(request)
            password = request.get("params", {}).get("password")
            if password != harness.password:
                self._write({"id": request.get("id"), "ok": False})
                return
            self._write({"id": request.get("id"), "ok": True, "result": {}})
            request = self._read_request()

        method = request.get("method")
        if method == "events.stream":
            harness.stream_requests.append(request)
            try:
                frames = harness.stream_scripts.pop(0)
            except IndexError:
                self._write(
                    {
                        "type": "error",
                        "ok": False,
                        "error": {"code": "unexpected_connection"},
                    }
                )
                return
            for frame in frames:
                self._write(frame)
            return

        harness.command_requests.append(request)
        if harness.command_handler is not None:
            response = harness.command_handler(request)
        else:
            response = {"ok": True, "result": {"method": method}}
        self._write({**response, "id": request.get("id")})

    def _read_request(self) -> dict[str, object]:
        raw = self.rfile.readline()
        value = json.loads(raw)
        assert isinstance(value, dict)
        return value

    def _write(self, value: dict[str, object]) -> None:
        self.wfile.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class CmuxSocketHarness:
    """Real Unix listener with scripted event-stream frames."""

    def __init__(
        self,
        path: str | Path,
        stream_scripts: Iterable[Iterable[dict[str, object]]],
        *,
        password: str | None = None,
        command_handler: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> None:
        self.path = str(path)
        self.stream_scripts = [list(script) for script in stream_scripts]
        self.password = password
        self.stream_requests: list[dict[str, object]] = []
        self.command_requests: list[dict[str, object]] = []
        # Backward-compatible name: snapshots are command RPCs too.
        self.snapshot_requests = self.command_requests
        self.auth_attempts: list[dict[str, object]] = []
        self.command_handler = command_handler
        self._server = _Server(self.path, _Handler)
        self._server.harness = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> CmuxSocketHarness:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        Path(self.path).unlink(missing_ok=True)


def ack(
    boot_id: str,
    *,
    replay_count: int,
    gap: bool,
    requested_after_seq: int | None,
    latest_seq: int,
) -> dict[str, object]:
    return {
        "type": "ack",
        "protocol": "cmux-events",
        "version": 1,
        "boot_id": boot_id,
        "replay_count": replay_count,
        "resume": {
            "after_seq": requested_after_seq,
            "requested_after_seq": requested_after_seq,
            "oldest_seq": max(0, latest_seq - replay_count + 1),
            "latest_seq": latest_seq,
            "next_seq": latest_seq + 1,
            "gap": gap,
        },
    }


def event(
    boot_id: str,
    seq: int,
    *,
    name: str = "workspace.focused",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "event",
        "protocol": "cmux-events",
        "version": 1,
        "boot_id": boot_id,
        "seq": seq,
        "id": f"{boot_id}-{seq}",
        "name": name,
        "category": name.split(".", 1)[0],
        "source": "test.cmux",
        "occurred_at": "2026-07-19T00:00:00Z",
        "workspace_id": "workspace-test",
        "surface_id": "surface-test",
        "pane_id": "pane-test",
        "window_id": "window-test",
        "payload": payload or {},
    }


def slow_consumer(latest_seq: int) -> dict[str, object]:
    return {
        "type": "error",
        "ok": False,
        "error": {
            "code": "slow_consumer",
            "message": "subscriber exceeded 1024 pending events",
            "latest_seq": latest_seq,
        },
    }
