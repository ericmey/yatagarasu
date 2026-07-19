"""Dedicated Unix-socket client for CMUX events and gap snapshots."""

from __future__ import annotations

import json
import socket
from contextlib import AbstractContextManager
from itertools import count
from pathlib import Path
from types import TracebackType

from .event_outbox import SnapshotBaseline
from .stream_protocol import MAX_FRAME_BYTES, StreamProtocolError

SNAPSHOT_METHODS: tuple[str, str, str] = (
    "extension.sidebar.snapshot",
    "workspace.list",
    "system.tree",
)
MAX_COMMAND_FRAME_BYTES = 4 * 1024 * 1024


class StreamConnection(AbstractContextManager["StreamConnection"]):
    """One connection permanently taken over by ``events.stream``."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._reader = sock.makefile("rb")

    def set_read_timeout(self, seconds: float | None) -> None:
        """Bound how long a single ``read_frame`` may block.

        The resident deliberately runs with no timeout — it is supposed to wait
        indefinitely for the next event. A preflight check is the opposite: it
        must terminate, or it is not a check. A timeout raises out of
        ``read_frame`` and leaves the buffered reader mid-frame, so a caller
        that sets one must treat the timeout as terminal for this connection
        rather than looping and reading again.
        """
        self._socket.settimeout(seconds)

    def read_frame(self) -> dict[str, object] | None:
        raw = self._reader.readline(MAX_FRAME_BYTES + 2)
        if not raw:
            return None
        if len(raw) > MAX_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
            raise StreamProtocolError("CMUX stream frame exceeds the 16 KiB limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StreamProtocolError("CMUX stream frame is not valid JSON") from exc
        if not isinstance(value, dict):
            raise StreamProtocolError("CMUX stream frame must be a JSON object")
        return value

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._reader.close()
        self._socket.close()


class UnixCmuxSocketClient:
    """Open a dedicated stream socket and separate command sockets."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        password: str | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.socket_path = str(socket_path)
        self.password = password
        self.connect_timeout_s = connect_timeout_s
        self._request_ids = count(1)

    def call(self, method: str, params: dict[str, object] | None = None) -> object:
        """Execute one v2 RPC on a fresh non-stream command connection.

        Notification and admission operations must never borrow the resident's
        stream socket: one delayed command response would otherwise stop event
        consumption and manufacture a slow-consumer disconnect.
        """
        request_id = f"yatagarasu-command-{next(self._request_ids)}"
        sock = self._connect()
        try:
            with sock.makefile("rb") as reader:
                self._authenticate(sock, reader)
                self._write_json(
                    sock,
                    {
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    },
                )
                response = self._read_json(reader)
        finally:
            sock.close()
        if response.get("id") != request_id:
            raise StreamProtocolError("CMUX command response id mismatch")
        if response.get("ok") is not True:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else "unknown"
            raise StreamProtocolError(f"CMUX command failed: {method}: {code}")
        return response.get("result")

    def open_stream(self, *, after_seq: int | None) -> StreamConnection:
        sock = self._connect()
        try:
            reader = sock.makefile("rb")
            try:
                self._authenticate(sock, reader)
                params: dict[str, object] = {"include_heartbeats": True}
                if after_seq is not None:
                    params["after_seq"] = after_seq
                self._write_json(
                    sock,
                    {
                        "id": "yatagarasu-events",
                        "method": "events.stream",
                        "params": params,
                    },
                )
            finally:
                reader.close()
            sock.settimeout(None)
            return StreamConnection(sock)
        except BaseException:
            sock.close()
            raise

    def snapshots(
        self, methods: tuple[str, ...] = SNAPSHOT_METHODS
    ) -> tuple[SnapshotBaseline, ...]:
        """Run literal snapshot RPCs on command connections, never the stream."""
        completed: list[SnapshotBaseline] = []
        for index, method in enumerate(methods):
            sock = self._connect()
            try:
                with sock.makefile("rb") as reader:
                    self._authenticate(sock, reader)
                    self._write_json(
                        sock,
                        {
                            "id": f"yatagarasu-snapshot-{index}",
                            "method": method,
                            "params": {},
                        },
                    )
                    response = self._read_json(reader)
                    if response.get("ok") is not True:
                        raise StreamProtocolError(f"snapshot RPC failed: {method}")
                    completed.append(
                        SnapshotBaseline(
                            method,
                            json.dumps(
                                response.get("result"),
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    )
            finally:
                sock.close()
        return tuple(completed)

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout_s)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        return sock

    def _authenticate(self, sock: socket.socket, reader) -> None:
        if self.password is None:
            return
        self._write_json(
            sock,
            {
                "id": "yatagarasu-auth",
                "method": "auth.login",
                "params": {"password": self.password},
            },
        )
        response = self._read_json(reader)
        if response.get("ok") is not True:
            raise StreamProtocolError("CMUX socket authentication failed")

    @staticmethod
    def _write_json(sock: socket.socket, value: dict[str, object]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        sock.sendall(payload)

    @staticmethod
    def _read_json(reader) -> dict[str, object]:
        raw = reader.readline(MAX_COMMAND_FRAME_BYTES + 2)
        if not raw or len(raw) > MAX_COMMAND_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
            raise StreamProtocolError("CMUX command response is missing or oversized")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StreamProtocolError(
                "CMUX command response is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise StreamProtocolError("CMUX command response must be a JSON object")
        return value
