"""Tests for the session state machine.

These cover the rule the whole design exists for: a Bluetooth failure changes
the *session*, never the uinput device.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from buib_protocol import SessionState
from buibd.bluetooth import BluetoothError
from buibd.session import HidSession, SessionError
from descriptors import BOOT_KEYBOARD, GAMEPAD
from fakes import FakeController, FakeEventSink


@pytest.fixture
def changes() -> list[None]:
    return []


def make_session(
    controller: FakeController, sink: FakeEventSink, changes: list[None] | None = None
) -> HidSession:
    return HidSession(
        sink,
        controller,
        on_change=(lambda: changes.append(None)) if changes is not None else None,
    )


async def connect(session: HidSession, address: str = "E1:CA:72:48:C4:E8") -> None:
    """Start a connection and wait for it, whatever the outcome."""
    with contextlib.suppress(SessionError):
        # The outcome is in the status, which is what these tests check.
        await session.connect(address, wait=True)


async def test_initial_state(sink: FakeEventSink, controller: FakeController) -> None:
    session = make_session(controller, sink)
    assert session.status.state is SessionState.DISCONNECTED
    assert session.status.address is None
    assert not session.busy


async def test_connect_reports_the_device(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)

    assert session.status.state is SessionState.CONNECTED
    assert session.status.address == "E1:CA:72:48:C4:E8"
    assert session.status.name == controller.device_name
    assert session.status.reports == 0
    assert session.status.to_dict()["device"]["battery_level"] == 87
    assert session.status.connected_at is not None


async def test_connect_waits_for_the_outcome(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    assert not session.busy
    assert session.status.state is SessionState.CONNECTED


async def test_connect_without_wait_returns_before_the_outcome(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await session.connect("E1:CA:72:48:C4:E8")
    assert session.status.state is SessionState.CONNECTING
    assert session.busy

    while session.busy:
        await asyncio.sleep(0)
    assert session.status.state is SessionState.CONNECTED


async def test_reports_reach_the_sink(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)

    controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))
    controller.send_report(bytes([0, 0, 0, 0, 0, 0, 0, 0]))

    assert sink.names() == [("KEY_A", 1), ("KEY_A", 0)]
    assert session.status.reports == 2
    assert session.status.to_dict()["reports"] == 2


async def test_a_failed_connection_leaves_the_device_alone(sink: FakeEventSink) -> None:
    controller = FakeController(BOOT_KEYBOARD, fail_with="adapter is not present")
    session = make_session(controller, sink)

    await connect(session)

    assert session.status.state is SessionState.DISCONNECTED
    assert "adapter is not present" in (session.status.error or "")
    assert not sink.closed
    assert sink.events == []


async def test_waiting_for_a_failed_connection_raises(sink: FakeEventSink) -> None:
    controller = FakeController(BOOT_KEYBOARD, fail_with="timed out")
    session = make_session(controller, sink)

    with pytest.raises(SessionError, match="timed out"):
        await session.connect("E1:CA:72:48:C4:E8", wait=True)
    assert session.status.state is SessionState.DISCONNECTED


async def test_an_unparsable_descriptor_is_a_session_error(sink: FakeEventSink) -> None:
    controller = FakeController(b"\x0c\x01", device_name="Broken")
    session = make_session(controller, sink)

    await connect(session)

    assert session.status.state is SessionState.DISCONNECTED
    assert isinstance(session.status.error, str)
    assert not sink.closed


async def test_channels_that_fail_to_open_are_reported(sink: FakeEventSink) -> None:
    controller = FakeController(BOOT_KEYBOARD, channels_fail="no interrupt channel")
    session = make_session(controller, sink)

    await connect(session)

    assert session.status.state is SessionState.DISCONNECTED
    assert "no interrupt channel" in (session.status.error or "")
    assert not sink.closed


async def test_unsupported_usages_are_reported(sink: FakeEventSink) -> None:
    """A gamepad against a keyboard only sink cannot press its buttons."""
    keyboard_only = FakeEventSink(profile="keyboard")
    controller = FakeController(GAMEPAD, device_name="Fancy Gamepad")
    session = make_session(controller, keyboard_only)

    await connect(session)

    assert session.status.state is SessionState.CONNECTED
    assert session.status.unsupported
    assert any("BTN_SOUTH" in usage for usage in session.status.unsupported)


async def test_disconnect_releases_held_keys(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    controller.send_report(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))
    assert sink.pressed

    await session.disconnect()

    assert session.status.state is SessionState.DISCONNECTED
    assert session.status.address is None
    assert not sink.pressed
    assert sink.released == 1  # the converter released, and so did the sink
    assert not sink.closed
    assert controller.link is None


async def test_disconnect_when_not_connected(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    with pytest.raises(SessionError, match="not connected"):
        await session.disconnect()


async def test_connecting_twice_is_rejected(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    with pytest.raises(SessionError, match="already connected"):
        await session.connect("11:22:33:44:55:66")


async def test_a_dropped_link_is_handled(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    controller.send_report(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))

    controller.drop_link("the device disconnected (reason 0x13)")
    await asyncio.sleep(0)  # let the teardown task run
    await asyncio.sleep(0)

    assert session.status.state is SessionState.DISCONNECTED
    assert "disconnected" in (session.status.error or "")
    assert not sink.pressed
    assert not sink.closed


async def test_reports_after_a_teardown_are_ignored(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    await session.disconnect()

    controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))
    assert sink.events == []


async def test_a_failing_converter_does_not_kill_the_session(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)

    async def explode() -> None:
        raise RuntimeError("boom")

    # A sink that misbehaves must not take the daemon down.
    sink.emit = explode  # type: ignore[method-assign]
    controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))

    assert session.status.state is SessionState.CONNECTED
    sink.emit = FakeEventSink.emit.__get__(sink)  # type: ignore[method-assign]
    controller.send_report(bytes([0, 0, 0, 5, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_B", 1)]


async def test_connect_after_a_failure_works(sink: FakeEventSink) -> None:
    controller = FakeController(BOOT_KEYBOARD, fail_with="no such device")
    session = make_session(controller, sink)
    await connect(session)
    assert session.status.state is SessionState.DISCONNECTED

    controller.fail_with = None
    await connect(session)
    assert session.status.state is SessionState.CONNECTED
    assert session.status.error is None


async def test_disconnect_cancels_a_connect_that_is_still_running(
    sink: FakeEventSink,
) -> None:
    class SlowController(FakeController):
        async def connect_hid(self, address: str, **kwargs: object) -> object:
            await asyncio.sleep(10)
            raise BluetoothError("never mind")

    controller = SlowController(BOOT_KEYBOARD)
    session = make_session(controller, sink)
    await session.connect("E1:CA:72:48:C4:E8")
    assert session.busy

    await session.disconnect()

    assert not session.busy
    assert session.status.state is SessionState.DISCONNECTED


async def test_close_releases_everything(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await connect(session)
    controller.send_report(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))

    await session.close()

    assert not sink.pressed
    assert not sink.closed  # the owner of the sink closes it, not the session
    assert controller.closed is False


async def test_connect_after_close_is_rejected(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    await session.close()
    with pytest.raises(SessionError, match="shutting down"):
        await session.connect("E1:CA:72:48:C4:E8")


async def test_change_callback_is_called(
    sink: FakeEventSink, controller: FakeController, changes: list[None]
) -> None:
    session = make_session(controller, sink, changes)
    await connect(session)
    assert changes, "the session should report its state changes"

    await session.disconnect()
    assert len(changes) >= 2


async def test_status_data_omits_what_it_does_not_know(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)
    data = session.status.to_dict()
    assert data == {
        "state": "disconnected",
        "address": None,
        "name": None,
        "error": None,
        "reports": 0,
    }


async def test_a_bad_address_is_rejected_before_anything_happens(
    sink: FakeEventSink, controller: FakeController
) -> None:
    session = make_session(controller, sink)

    with pytest.raises(SessionError, match="invalid Bluetooth address"):
        await session.connect("not-an-address")

    assert session.status.state is SessionState.DISCONNECTED
    assert controller.link is None


# -- diagnostics ------------------------------------------------------------


async def test_a_failed_connection_logs_a_traceback(
    sink: FakeEventSink, caplog: pytest.LogCaptureFixture
) -> None:
    """A one line message is rarely enough to diagnose a stack failure."""
    controller = FakeController(BOOT_KEYBOARD, fail_with="Expecting value: line 1")
    session = make_session(controller, sink)

    with caplog.at_level("WARNING"):
        await connect(session)

    record = next(r for r in caplog.records if "failed" in r.message)
    assert record.levelname == "WARNING"
    assert record.exc_info is not None, "the traceback should be logged"
    assert "BluetoothError" in caplog.text


async def test_the_traceback_can_be_turned_off(
    sink: FakeEventSink, caplog: pytest.LogCaptureFixture
) -> None:
    controller = FakeController(BOOT_KEYBOARD, fail_with="Expecting value: line 1")
    session = HidSession(sink, controller, log_traceback=False)

    with caplog.at_level("WARNING"):
        await connect(session)

    record = next(r for r in caplog.records if "failed" in r.message)
    assert record.exc_info is None
    assert "Expecting value" in caplog.text


async def test_the_client_only_ever_sees_one_line(
    sink: FakeEventSink, caplog: pytest.LogCaptureFixture
) -> None:
    """The response stays short even when the log gets the whole traceback."""
    controller = FakeController(BOOT_KEYBOARD, fail_with="Expecting value: line 1")
    session = make_session(controller, sink)
    with caplog.at_level("WARNING"):
        await connect(session)

    assert "\n" not in (session.status.error or "")
