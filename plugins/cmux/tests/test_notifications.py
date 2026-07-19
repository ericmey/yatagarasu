from __future__ import annotations

import pytest
from yatagarasu_cmux import (
    NotificationLifecycle,
    NotificationLifecycleError,
    UnixCmuxSocketClient,
    validate_hook_effects,
)

from .socket_harness import CmuxSocketHarness, short_socket_path
from .test_acceptance_006_010 import _NotificationCmux


def _publish(lifecycle, suffix: str):
    return lifecycle.publish(
        event_id=f"event-{suffix}",
        delivery_id=f"delivery-{suffix}",
        seat_id="yua",
        workspace_id="workspace-yua",
        surface_id="surface-yua",
        title="Yatagarasu",
        body=f"message {suffix}",
    )


def test_setting_is_a_hard_prerequisite(tmp_path):
    with pytest.raises(
        NotificationLifecycleError,
        match=r"notifications\.suppressOnlyFocusedSurface must be true",
    ):
        NotificationLifecycle(
            tmp_path / "notifications.sqlite",
            client=None,  # type: ignore[arg-type]
            suppress_only_focused_surface=False,
        )


def test_prepared_crash_reconciles_without_duplicate_banner(tmp_path):
    socket_path = short_socket_path(tmp_path, "notification-recovery")
    cmux = _NotificationCmux()
    db_path = tmp_path / "notifications.sqlite"
    with CmuxSocketHarness(socket_path, [], command_handler=cmux.handle):
        client = UnixCmuxSocketClient(socket_path)
        with NotificationLifecycle(
            db_path,
            client=client,
            suppress_only_focused_surface=True,
            clock=lambda: 1.0,
        ) as lifecycle:
            first = _publish(lifecycle, "recovery")

        # Model the durable side of the crash boundary: CMUX has the banner but
        # the local row did not reach active. Retry must discover the token.
        import sqlite3

        db = sqlite3.connect(db_path)
        db.execute(
            "UPDATE notification_lifecycle SET state='prepared', notification_id=NULL"
        )
        db.commit()
        db.close()

        with NotificationLifecycle(
            db_path,
            client=client,
            suppress_only_focused_surface=True,
            clock=lambda: 2.0,
        ) as recovered:
            second = _publish(recovered, "recovery")

    assert first.notification_id == second.notification_id
    assert len(cmux.notifications) == 1


def test_event_id_reuse_with_changed_content_is_a_named_contradiction(tmp_path):
    socket_path = short_socket_path(tmp_path, "notification-contradiction")
    cmux = _NotificationCmux()
    with (
        CmuxSocketHarness(socket_path, [], command_handler=cmux.handle),
        NotificationLifecycle(
            tmp_path / "notifications.sqlite",
            client=UnixCmuxSocketClient(socket_path),
            suppress_only_focused_surface=True,
        ) as lifecycle,
    ):
        _publish(lifecycle, "same-event")
        with pytest.raises(
            NotificationLifecycleError, match="event_id_claim_contradiction"
        ):
            lifecycle.publish(
                event_id="event-same-event",
                delivery_id="delivery-same-event",
                seat_id="yua",
                workspace_id="workspace-yua",
                surface_id="surface-yua",
                title="Yatagarasu",
                body="changed body",
            )

    assert len(cmux.notifications) == 1


def test_broadcast_event_keeps_one_mapping_per_delivery(tmp_path):
    socket_path = short_socket_path(tmp_path, "notification-broadcast")
    cmux = _NotificationCmux()
    with (
        CmuxSocketHarness(socket_path, [], command_handler=cmux.handle),
        NotificationLifecycle(
            tmp_path / "nested" / "state" / "notifications.sqlite",
            client=UnixCmuxSocketClient(socket_path),
            suppress_only_focused_surface=True,
        ) as lifecycle,
    ):
        first = lifecycle.publish(
            event_id="event-broadcast",
            delivery_id="delivery-yua",
            seat_id="yua",
            workspace_id="workspace-yua",
            surface_id="surface-yua",
            title="Yatagarasu",
            body="shared event",
        )
        second = lifecycle.publish(
            event_id="event-broadcast",
            delivery_id="delivery-aoi",
            seat_id="aoi",
            workspace_id="workspace-aoi",
            surface_id="surface-aoi",
            title="Yatagarasu",
            body="shared event",
        )
        assert lifecycle.on_receipt(
            event_id="event-broadcast",
            delivery_id="delivery-yua",
            status="accepted",
            state="in-session",
            evidence_class="harness.prompt_accepted",
        )
        active = lifecycle.active_records()

    assert first.notification_id != second.notification_id
    assert [row.delivery_id for row in active] == ["delivery-aoi"]
    assert [item["id"] for item in cmux.notifications] == [second.notification_id]


def test_per_seat_cap_and_ttl_retire_exact_notifications(tmp_path):
    socket_path = short_socket_path(tmp_path, "notification-retention")
    cmux = _NotificationCmux()
    now = [10.0]
    with (
        CmuxSocketHarness(socket_path, [], command_handler=cmux.handle),
        NotificationLifecycle(
            tmp_path / "notifications.sqlite",
            client=UnixCmuxSocketClient(socket_path),
            suppress_only_focused_surface=True,
            per_seat_cap=2,
            mailbox_ttl_s=5,
            clock=lambda: now[0],
        ) as lifecycle,
    ):
        first = _publish(lifecycle, "one")
        now[0] = 11.0
        second = _publish(lifecycle, "two")
        now[0] = 12.0
        third = _publish(lifecycle, "three")
        assert [row.notification_id for row in lifecycle.active_records()] == [
            second.notification_id,
            third.notification_id,
        ]
        now[0] = 20.0
        assert lifecycle.expire() == 2

    assert first.notification_id not in {item["id"] for item in cmux.notifications}
    assert cmux.notifications == []


def test_exact_mark_read_retires_only_its_mapping(tmp_path):
    socket_path = short_socket_path(tmp_path, "notification-mark-read")
    cmux = _NotificationCmux()
    with (
        CmuxSocketHarness(socket_path, [], command_handler=cmux.handle),
        NotificationLifecycle(
            tmp_path / "notifications.sqlite",
            client=UnixCmuxSocketClient(socket_path),
            suppress_only_focused_surface=True,
        ) as lifecycle,
    ):
        first = _publish(lifecycle, "read")
        second = _publish(lifecycle, "unread")
        assert lifecycle.on_notification_read("unknown") is False
        assert lifecycle.on_notification_read(first.notification_id) is True
        assert [row.notification_id for row in lifecycle.active_records()] == [
            second.notification_id
        ]


def test_hook_cannot_silently_remove_desktop_or_unread_effects():
    before = {"effects": {"desktop": True, "markUnread": True, "sound": True}}
    validate_hook_effects(before, before)
    with pytest.raises(NotificationLifecycleError, match="removed_desktop"):
        validate_hook_effects(before, {"effects": {"markUnread": True}})
    with pytest.raises(NotificationLifecycleError, match="removed_mark_unread"):
        validate_hook_effects(before, {"effects": {"desktop": True}})
    validate_hook_effects(
        before,
        {"effects": {"desktop": True, "markUnread": False}},
        explicit_mark_read=True,
    )
