"""Command line entry point for the cmux transport resident.

Three subcommands, in the order an operator actually needs them:

``doctor``
    Resolve and validate configuration, print it, touch nothing. The first thing
    to run on a new host, and safe to run at any time.

``smoke``
    Prove the resident can reach a live cmux and read its event stream. Read-only:
    it never injects. This is what ends "it has never run".

``run``
    Supervise the resident.

Every path fails loudly rather than degrading. A transport that starts without a
usable socket reports healthy and delivers nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import runtime
from .runtime import RuntimeDiscoveryError
from .socket_client import UnixCmuxSocketClient
from .supervisor import Supervisor, marker_key_path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", help="cmux control socket (default: discovered)")
    parser.add_argument("--state-dir", help="durable state directory")
    parser.add_argument(
        "--source-instance-id",
        help="stable identity of this resident; half of the cursor key",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yatagarasu-cmux", description=__doc__.split("\n")[0]
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("doctor", "validate configuration and print it; changes nothing"),
        ("smoke", "read the live event stream to prove connectivity; never injects"),
        ("run", "supervise the resident"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common(p)
        if name == "run":
            p.add_argument(
                "--max-connections",
                type=int,
                default=8,
                help="bounded reconnect budget for this pass",
            )
        if name == "smoke":
            p.add_argument("--max-frames", type=int, default=25)
            p.add_argument("--timeout", type=float, default=15.0)
    return parser


def _load(args: argparse.Namespace) -> runtime.RuntimeConfig:
    return runtime.load(
        socket_path=args.socket,
        state_dir=args.state_dir,
        source_instance_id=args.source_instance_id,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    print(runtime.describe(config))
    key = marker_key_path(config)
    print(f"marker_key={key} ({'present' if key.exists() else 'not yet minted'})")
    print("\nconfiguration is usable; nothing was started")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """Prove we can reach cmux and read real events. Never injects.

    Reads a bounded number of frames directly rather than going through
    ``EventStreamResident.run()``. The resident is deliberately shaped for
    production — it sets no read timeout and only exits on stream end or a
    slow-consumer disconnect — which is correct there and never terminates
    here. A preflight check must be bounded or it is not a check.
    """
    config = _load(args)
    print(runtime.describe(config))
    print(
        f"\nreading up to {args.max_frames} frames from the live stream"
        f" (read-only, {args.timeout}s budget)..."
    )

    client = UnixCmuxSocketClient(config.socket_path, password=config.password)
    seen: dict[str, int] = {}
    frames = 0
    deadline = time.monotonic() + args.timeout

    with client.open_stream(after_seq=None) as stream:
        # Terminal, not per-read: a timeout raises mid-frame and leaves the
        # buffered reader misaligned, so we stop rather than read again.
        stream.set_read_timeout(args.timeout)
        while frames < args.max_frames and time.monotonic() < deadline:
            try:
                frame = stream.read_frame()
            except (TimeoutError, OSError):
                break
            if frame is None:
                break
            frames += 1
            kind = str(frame.get("type") or "?")
            name = str(frame.get("name") or "")
            key = f"{kind}:{name}" if name else kind
            seen[key] = seen.get(key, 0) + 1

    print(f"  frames_read = {frames}")
    for key in sorted(seen, key=lambda k: -seen[k]):
        print(f"    {seen[key]:4d}  {key}")

    if frames == 0:
        print("\nFAILED: connected but read no frames", file=sys.stderr)
        return 1
    print("\nRead live frames from cmux. No injection was performed.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    log.info("starting resident: %s", config.source_instance_id)
    result = Supervisor(config).run_once(max_connections=args.max_connections)
    log.info(
        "resident pass finished: connections=%d inserted=%d duplicates=%d",
        result.connections,
        result.inserted_event_count,
        result.duplicate_event_count,
    )
    return 0


log = logging.getLogger("yatagarasu_cmux")

_COMMANDS = {"doctor": cmd_doctor, "smoke": cmd_smoke, "run": cmd_run}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return _COMMANDS[args.command](args)
    except RuntimeDiscoveryError as exc:
        # Loud, specific, and actionable — never a traceback for a config problem.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
