"""Behavioral tests for the reconnectable CMUX event resident."""

from __future__ import annotations

import json
import sqlite3

import pytest
from yatagarasu_cmux import (
    CommitDisposition,
    DerivedEvent,
    EventCursor,
    EventOutbox,
    EventStreamResident,
    OutboxError,
    SnapshotBaseline,
    StreamProtocolError,
    UnixCmuxSocketClient,
)

from yatagarasu_core import Delivery, DeliveryMode
from yatagarasu_core.proofs import MarkerAuthority

from .socket_harness import (
    CmuxSocketHarness,
    ack,
    event,
    short_socket_path,
    slow_consumer,
)

SOURCE = "cmux-resident-vesper"
KEY = b"event-resident-test-key"


def derived(boot_id: str, seq: int, *, event_id: str | None = None) -> DerivedEvent:
    return DerivedEvent(
        source_instance_id=SOURCE,
        boot_id=boot_id,
        seq=seq,
        source_event_id=event_id or f"{boot_id}-{seq}",
        event_name="workspace.focused",
        event_json=json.dumps({"boot_id": boot_id, "seq": seq}),
    )


def test_event_and_cursor_commit_are_atomic(tmp_path) -> None:
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        outbox._db.execute(
            """CREATE TRIGGER fail_cursor BEFORE INSERT ON stream_cursor
               BEGIN SELECT RAISE(ABORT, 'cursor write failed'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="cursor write failed"):
            outbox.commit_event(derived("boot-a", 1), permit_boot_change=False)

        assert outbox.outbox_rows(SOURCE) == ()
        assert outbox.cursor(SOURCE) is None


def test_gap_baseline_and_cursor_commit_are_atomic(tmp_path) -> None:
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        outbox.commit_event(derived("boot-old", 9), permit_boot_change=False)
        outbox._db.execute(
            """CREATE TRIGGER fail_snapshot_audit BEFORE INSERT ON stream_audit
               BEGIN SELECT RAISE(ABORT, 'snapshot audit failed'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot audit failed"):
            outbox.record_resnapshot(
                EventCursor(SOURCE, "boot-new", 2),
                snapshots=(SnapshotBaseline("system.tree", '{"tree":[]}'),),
                replay_count=2,
            )

        assert outbox.snapshot_rows(SOURCE) == ()
        assert outbox.cursor(SOURCE) == EventCursor(SOURCE, "boot-old", 9)


def test_cursor_identity_is_the_full_source_boot_sequence_triple(tmp_path) -> None:
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        assert (
            outbox.commit_event(derived("boot-a", 7), permit_boot_change=False)
            is CommitDisposition.INSERTED
        )
        other = DerivedEvent(
            source_instance_id="cmux-resident-mizuki",
            boot_id="boot-a",
            seq=7,
            source_event_id="boot-a-7",
            event_name="workspace.focused",
            event_json=json.dumps({"boot_id": "boot-a", "seq": 7}),
        )
        assert (
            outbox.commit_event(other, permit_boot_change=False)
            is CommitDisposition.INSERTED
        )

        assert outbox.cursor(SOURCE) == EventCursor(SOURCE, "boot-a", 7)
        assert outbox.cursor(other.source_instance_id) == EventCursor(
            other.source_instance_id, "boot-a", 7
        )


def test_duplicate_triple_is_idempotent_but_contradiction_is_named(tmp_path) -> None:
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        original = derived("boot-a", 1)
        outbox.commit_event(original, permit_boot_change=False)
        assert (
            outbox.commit_event(original, permit_boot_change=False)
            is CommitDisposition.DUPLICATE
        )

        contradiction = DerivedEvent(
            SOURCE,
            "boot-a",
            1,
            "different-id",
            "workspace.closed",
            json.dumps({"different": True}),
        )
        with pytest.raises(OutboxError, match="different evidence"):
            outbox.commit_event(contradiction, permit_boot_change=False)


def test_boot_change_requires_gap_authority(tmp_path) -> None:
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        outbox.commit_event(derived("boot-a", 9), permit_boot_change=False)
        with pytest.raises(OutboxError, match="without a declared resume gap"):
            outbox.commit_event(derived("boot-b", 1), permit_boot_change=False)


def test_gap_processes_replay_then_snapshots_on_separate_connections(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "gap")
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        outbox.commit_event(derived("boot-old", 9), permit_boot_change=False)
        scripts = [
            [
                ack(
                    "boot-new",
                    replay_count=2,
                    gap=True,
                    requested_after_seq=9,
                    latest_seq=2,
                ),
                event("boot-new", 1),
                event("boot-new", 2),
            ]
        ]
        with CmuxSocketHarness(socket_path, scripts) as harness:
            run = EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
            ).run()

        assert run.replay_event_count == 2
        assert run.snapshot_commands == (
            "extension.sidebar.snapshot",
            "workspace.list",
            "system.tree",
        )
        assert [request["method"] for request in harness.snapshot_requests] == list(
            run.snapshot_commands
        )
        assert outbox.cursor(SOURCE) == EventCursor(SOURCE, "boot-new", 2)
        baseline = outbox.snapshot_rows(SOURCE)
        assert [row["method"] for row in baseline] == sorted(run.snapshot_commands)
        assert {row["boot_id"] for row in baseline} == {"boot-new"}
        assert {row["seq"] for row in baseline} == {2}
        audit = outbox.audit_rows(SOURCE)
        assert [row["kind"] for row in audit] == [
            "gap_resnapshot",
            "reconnect_replay",
        ]


def test_zero_tail_gap_uses_new_boot_latest_sequence(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "zero-tail-gap")
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        outbox.commit_event(derived("boot-old", 900), permit_boot_change=False)
        scripts = [
            [
                ack(
                    "boot-new",
                    replay_count=0,
                    gap=True,
                    requested_after_seq=900,
                    latest_seq=0,
                ),
                event("boot-new", 1),
            ]
        ]
        with CmuxSocketHarness(socket_path, scripts):
            EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
            ).run()

        assert outbox.cursor(SOURCE) == EventCursor(SOURCE, "boot-new", 1)
        assert {row["seq"] for row in outbox.snapshot_rows(SOURCE)} == {0}


def test_socket_authentication_precedes_stream_subscription(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "auth")
    scripts = [
        [
            ack(
                "boot-auth",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=0,
            )
        ]
    ]
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, scripts, password="swordfish") as harness:
            EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path, password="swordfish"),
                outbox=outbox,
            ).run()

        assert len(harness.auth_attempts) == 1
        assert harness.stream_requests[0]["method"] == "events.stream"


def test_one_hundred_reconnects_resume_without_reinjecting_or_skipping(
    tmp_path,
) -> None:
    """Exercise the Y-CMUX-006 cursor invariant across 100 reconnect cycles."""
    socket_path = short_socket_path(tmp_path, "hundred-reconnects")
    scripts: list[list[dict[str, object]]] = [
        [
            ack(
                "boot-stable",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=1,
            ),
            event("boot-stable", 1),
            slow_consumer(1),
        ]
    ]
    for seq in range(2, 102):
        frames = [
            ack(
                "boot-stable",
                replay_count=1,
                gap=False,
                requested_after_seq=seq - 1,
                latest_seq=seq,
            ),
            event("boot-stable", seq),
        ]
        if seq < 101:
            frames.append(slow_consumer(seq))
        scripts.append(frames)

    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, scripts) as harness:
            run = EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
            ).run(max_connections=101)

        assert run.connections == 101
        assert run.reconnect_after_seq == (None, *range(1, 101))
        assert [
            request["params"].get("after_seq") for request in harness.stream_requests
        ] == [None, *range(1, 101)]
        assert run.replay_event_count == 100
        assert run.inserted_event_count == 101
        assert run.duplicate_event_count == 0
        assert run.snapshot_commands == ()
        assert outbox.cursor(SOURCE) == EventCursor(SOURCE, "boot-stable", 101)
        assert len(outbox.outbox_rows(SOURCE)) == 101


def test_sensitive_prompt_preview_is_not_persisted(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "private")

    authority = MarkerAuthority(KEY)
    delivery = Delivery(
        "ev-private",
        "delivery-private",
        "a-1",
        "b-1",
        "yua",
        DeliveryMode.SESSION_BOUND,
    )
    marker = authority.mint(
        delivery, issued_at="2026-07-18T21:00:00Z", expires_at="2026-07-18T21:05:00Z"
    )
    marker_text = authority.encode(marker)

    secret = f"{marker_text} private words that must never reach the outbox"
    scripts = [
        [
            ack(
                "boot-private",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=1,
            ),
            event(
                "boot-private",
                1,
                name="workspace.prompt.submitted",
                payload={
                    "message_preview": secret,
                    "message_length": len(secret),
                    "redacted_fields": ["message"],
                },
            ),
        ]
    ]
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, scripts):
            EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
            ).run()
        stored = outbox.outbox_rows(SOURCE)[0]["event_json"]

    assert "private words" not in stored
    assert marker_text in stored
    assert '"message_length"' in stored


def test_nested_safe_payload_values_are_not_persisted(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "nested-private")
    scripts = [
        [
            ack(
                "boot-private",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=1,
            ),
            event(
                "boot-private",
                1,
                payload={"redacted_fields": [{"secret": "must not persist"}]},
            ),
        ]
    ]
    with EventOutbox(tmp_path / "outbox.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, scripts):
            EventStreamResident(
                source_instance_id=SOURCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
            ).run()
        stored = outbox.outbox_rows(SOURCE)[0]["event_json"]

    assert "must not persist" not in stored
    assert "redacted_fields" not in stored


@pytest.mark.parametrize("latest_seq", [True, -1])
def test_malformed_slow_consumer_sequence_is_rejected(tmp_path, latest_seq) -> None:
    socket_path = short_socket_path(tmp_path, f"bad-slow-{latest_seq}")
    malformed = slow_consumer(1)
    error = malformed["error"]
    assert isinstance(error, dict)
    error["latest_seq"] = latest_seq
    scripts = [
        [
            ack(
                "boot-slow",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=0,
            ),
            malformed,
        ]
    ]
    with (
        EventOutbox(tmp_path / "outbox.sqlite") as outbox,
        CmuxSocketHarness(socket_path, scripts),
        pytest.raises(StreamProtocolError, match="latest sequence is malformed"),
    ):
        EventStreamResident(
            source_instance_id=SOURCE,
            client=UnixCmuxSocketClient(socket_path),
            outbox=outbox,
        ).run()


def test_oversized_event_frame_is_rejected(tmp_path) -> None:
    socket_path = short_socket_path(tmp_path, "oversized")
    oversized = event("boot-big", 1, payload={"safe": "x" * (16 * 1024 + 1)})
    scripts = [
        [
            ack(
                "boot-big",
                replay_count=0,
                gap=False,
                requested_after_seq=None,
                latest_seq=1,
            ),
            oversized,
        ]
    ]
    with (
        EventOutbox(tmp_path / "outbox.sqlite") as outbox,
        CmuxSocketHarness(socket_path, scripts),
        pytest.raises(StreamProtocolError, match="16 KiB"),
    ):
        EventStreamResident(
            source_instance_id=SOURCE,
            client=UnixCmuxSocketClient(socket_path),
            outbox=outbox,
        ).run()
