"""Production CMUX socket transport for one-shot terminal input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .socket_client import UnixCmuxSocketClient


@dataclass(frozen=True, slots=True)
class CmuxSocketTransport:
    """Send literal text and one submit key without changing focus.

    The transport deliberately exposes no read, focus, admission, or retry
    operation.  Receipt evidence decides the outcome after these two effects.
    """

    client: UnixCmuxSocketClient

    @classmethod
    def from_socket_path(
        cls,
        socket_path: str | Path,
        *,
        password: str | None = None,
    ) -> CmuxSocketTransport:
        return cls(UnixCmuxSocketClient(socket_path, password=password))

    def send_text(self, surface: str, text: str) -> None:
        self.client.call(
            "surface.send_text",
            {"surface_id": surface, "text": text},
        )

    def submit(self, surface: str, key: str) -> None:
        self.client.call(
            "surface.send_key",
            {"surface_id": surface, "key": key},
        )
