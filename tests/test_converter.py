"""Tests for the HID report to evdev event conversion."""

from __future__ import annotations

import pytest
from buibd.hid.converter import BindingKind, read_bits
from descriptors import (
    BOOT_KEYBOARD,
    GAMEPAD,
    MOUSE,
    MULTI_REPORT_KEYBOARD,
    MULTIMEDIA_KEYBOARD,
    TOUCHPAD,
)
from fakes import FakeEventSink

# -- bit reading ------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "offset", "size", "expected"),
    [
        (b"\xff", 0, 8, 0xFF),
        (b"\xff", 0, 4, 0x0F),
        (b"\xff", 4, 4, 0x0F),
        (b"\x81", 0, 8, 0x81),
        (b"\x00\x01", 8, 8, 1),
        (b"\x03\x00", 0, 1, 1),
        (b"\x03\x00", 1, 1, 1),
        (b"\x03\x00", 2, 1, 0),
    ],
)
def test_read_bits(data: bytes, offset: int, size: int, expected: int) -> None:
    assert read_bits(data, offset, size) == expected


def test_read_bits_sign_extends() -> None:
    assert read_bits(b"\x81", 0, 8, signed=True) == -127
    assert read_bits(b"\x7f", 0, 8, signed=True) == 127
    assert read_bits(b"\xff\xff", 0, 16, signed=True) == -1


def test_read_bits_past_the_end_raises() -> None:
    with pytest.raises(IndexError):
        read_bits(b"\x01", 8, 8)


# -- keyboard ---------------------------------------------------------------


def test_keyboard_modifier_bit(sink: FakeEventSink) -> None:
    sink.bind(BOOT_KEYBOARD)

    sink.handle(bytes([0x02, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_LEFTSHIFT", 1)]
    assert sink.syncs == 1

    # Pressing the same modifier again must not repeat the event.
    sink.clear()
    sink.handle(bytes([0x02, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.names() == []

    sink.clear()
    sink.handle(bytes([0, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_LEFTSHIFT", 0)]


def test_keyboard_key_array(sink: FakeEventSink) -> None:
    sink.bind(BOOT_KEYBOARD)

    # 'a' is usage 0x04, 'b' is 0x05: the report holds usage indexes.
    report = bytes([0, 0, 0, 4, 0, 0, 0, 0])
    sink.handle(report)
    assert sink.names() == [("KEY_A", 1)]

    # The key changed: release the old one and press the new one.
    sink.clear()
    sink.handle(bytes([0, 0, 0, 5, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_A", 0), ("KEY_B", 1)]

    # Releasing the slot releases whatever it held.
    sink.clear()
    sink.handle(bytes([0, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_B", 0)]


def test_keyboard_reports_roll_over_while_a_key_is_held(sink: FakeEventSink) -> None:
    sink.bind(BOOT_KEYBOARD)
    sink.handle(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_LEFTSHIFT", 1), ("KEY_A", 1)]

    sink.clear()
    sink.handle(bytes([0x02, 0, 0, 6, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_A", 0), ("KEY_C", 1)]


def test_keyboard_modifier_and_key_together(sink: FakeEventSink) -> None:
    sink.bind(BOOT_KEYBOARD)
    # Left control (bit 0 of the modifier byte) plus 'z' (usage 0x1D).
    sink.handle(bytes([0x01, 0, 0, 0x1D, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_LEFTCTRL", 1), ("KEY_Z", 1)]


def test_multi_report_keyboard_selects_by_report_id(sink: FakeEventSink) -> None:
    sink.bind(MULTI_REPORT_KEYBOARD)
    # Report 1: an ordinary key report.
    sink.handle(bytes([1, 0, 0, 4, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_A", 1)]

    # Report 2: six one bit keys, the first of which is F1.
    sink.clear()
    sink.handle(bytes([2, 0b000001, 0, 0, 0, 0, 0]))
    assert sink.names() == [("KEY_F1", 1)]


def test_unknown_report_id_is_reported_once(
    sink: FakeEventSink, caplog: pytest.LogCaptureFixture
) -> None:
    instance = sink.bind(MULTI_REPORT_KEYBOARD)
    sink.handle(bytes([7, 0, 0, 0, 0, 0, 0, 0]))
    sink.handle(bytes([7, 0, 0, 0, 0, 0, 0, 0]))
    assert sink.events == []
    assert instance.unsupported == {"input report id 7 is not in the report descriptor"}


def test_multimedia_keys(sink: FakeEventSink) -> None:
    sink.bind(MULTIMEDIA_KEYBOARD)
    # Mute is the first of the three consumer bits, which sit in byte 8.
    sink.handle(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0b001]))
    assert sink.names() == [("KEY_MUTE", 1)]

    sink.clear()
    sink.handle(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0b110]))
    assert sink.names() == [("KEY_MUTE", 0), ("KEY_CUT", 1), ("KEY_PLAYPAUSE", 1)]


# -- mouse ------------------------------------------------------------------


def test_mouse_buttons_and_axes(sink: FakeEventSink) -> None:
    sink.bind(MOUSE)
    # Left button, X = -5, Y = +3, wheel = 0.
    sink.handle(bytes([0b001, 0xFB, 0x03, 0x00]))
    assert sink.names() == [("BTN_LEFT", 1), ("REL_X", -5), ("REL_Y", 3)]


def test_mouse_releases_buttons(sink: FakeEventSink) -> None:
    sink.bind(MOUSE)
    sink.handle(bytes([0b011, 0, 0, 0]))
    assert sink.names() == [("BTN_LEFT", 1), ("BTN_RIGHT", 1)]

    sink.clear()
    sink.handle(bytes([0b010, 0, 0, 0]))
    assert sink.names() == [("BTN_LEFT", 0)]


def test_mouse_negative_values_are_signed(sink: FakeEventSink) -> None:
    sink.bind(MOUSE)
    sink.handle(bytes([0, 0x80, 0xFF, 0x7F]))
    assert sink.names() == [("REL_X", -128), ("REL_Y", -1), ("REL_WHEEL", 127)]


# -- gamepad ----------------------------------------------------------------


def pack(
    fields: dict[int, tuple[int, int]], length: int, report_id: int | None = None
) -> bytes:
    """Build a report from ``{bit offset: (value, bit width)}`` entries."""
    buffer = bytearray(length)
    for offset, (value, width) in fields.items():
        for index in range(width):
            if value & (1 << index):
                bit = offset + index
                buffer[bit >> 3] |= 1 << (bit & 7)
    return bytes([report_id, *buffer]) if report_id is not None else bytes(buffer)


def gamepad_report(
    buttons: int = 0,
    padding: int = 0,
    hat: int = 8,  # 8 is the null state, so unrelated tests see no d-pad events
    x: int = 0,
    y: int = 0,
    z: int = 0,
    rz: int = 0,
) -> bytes:
    """A 9 byte gamepad report, including its report ID byte."""
    return pack(
        {
            0: (buttons, 10),
            10: (padding, 2),
            12: (hat, 4),
            20: (x, 16),
            36: (y, 16),
            52: (z, 8),
            60: (rz, 8),
        },
        length=9,
        report_id=1,
    )


def test_gamepad_buttons(sink: FakeEventSink) -> None:
    sink.bind(GAMEPAD)
    sink.handle(gamepad_report(buttons=0b0000000001))
    assert sink.names() == [("BTN_SOUTH", 1)]

    sink.clear()
    sink.handle(gamepad_report(buttons=0b0000000011))
    assert sink.names() == [("BTN_EAST", 1)]

    sink.clear()
    sink.handle(gamepad_report(buttons=0))
    assert sink.names() == [("BTN_SOUTH", 0), ("BTN_EAST", 0)]


def test_gamepad_axes_are_scaled_into_the_absolute_range(sink: FakeEventSink) -> None:
    sink.bind(GAMEPAD)
    # The descriptor says 0..255, the uinput device says 0..65535.
    sink.handle(gamepad_report(x=0x80, y=0x80, z=0xFF, rz=0xFF))
    assert sink.names() == [
        ("ABS_X", 32896),
        ("ABS_Y", 32896),
        ("ABS_Z", 255),
        ("ABS_RZ", 255),
    ]

    # A value that did not change is not reported again.
    sink.clear()
    sink.handle(gamepad_report())
    assert sink.names() == [("ABS_X", 0), ("ABS_Y", 0), ("ABS_Z", 0), ("ABS_RZ", 0)]

    sink.clear()
    sink.handle(gamepad_report(x=0xFF, y=0xFF))
    assert sink.names() == [("ABS_X", 65535), ("ABS_Y", 65535)]


def test_gamepad_hat_switch(sink: FakeEventSink) -> None:
    """The usual numbering: 0 is north and the positions go clockwise."""
    instance = sink.bind(GAMEPAD)
    assert any(b.kind is BindingKind.HAT for b in instance.bindings)

    sink.handle(gamepad_report(hat=1))  # north east
    assert sink.names() == [("BTN_DPAD_UP", 1), ("BTN_DPAD_RIGHT", 1)]

    sink.clear()
    sink.handle(gamepad_report(hat=2))  # east: only the release is new
    assert sink.names() == [("BTN_DPAD_UP", 0)]  # right stays pressed

    sink.clear()
    sink.handle(gamepad_report(hat=4))  # south
    assert sink.names() == [("BTN_DPAD_RIGHT", 0), ("BTN_DPAD_DOWN", 1)]

    sink.clear()
    sink.handle(gamepad_report(hat=0))  # north
    assert sink.names() == [("BTN_DPAD_DOWN", 0), ("BTN_DPAD_UP", 1)]

    # The ninth position of a nine way hat is the null state.
    sink.clear()
    sink.handle(gamepad_report(hat=8))
    assert sink.names() == [("BTN_DPAD_UP", 0)]


def test_hat_switch_can_count_the_other_way(sink: FakeEventSink) -> None:
    sink.bind(GAMEPAD, hat_direction="ccw")
    sink.handle(gamepad_report(hat=2))  # west instead of east
    assert sink.names() == [("BTN_DPAD_LEFT", 1)]


def test_invalid_hat_direction_is_rejected(sink: FakeEventSink) -> None:
    with pytest.raises(ValueError, match="hat_direction"):
        sink.bind(GAMEPAD, hat_direction="sideways")


# -- touchpad ---------------------------------------------------------------


def touchpad_report(count: int, present: int, x: int, y: int) -> bytes:
    return pack({0: (count, 5), 6: (present, 1), 7: (x, 8), 15: (y, 8)}, length=3)


def test_touchpad_contact_lifecycle(sink: FakeEventSink) -> None:
    sink.bind(TOUCHPAD)

    sink.handle(touchpad_report(count=1, present=1, x=64, y=32))
    assert sink.names() == [
        ("ABS_MT_SLOT", 0),
        ("ABS_MT_TRACKING_ID", 1),
        ("ABS_MT_POSITION_X", 1032),  # 64/127 of the 0..2047 range
        ("ABS_MT_POSITION_Y", 516),
    ]

    # Moving without changing the contact state only moves the finger.
    sink.clear()
    sink.handle(touchpad_report(count=1, present=1, x=65, y=32))
    assert sink.names() == [("ABS_MT_POSITION_X", 1048)]

    # A report that repeats the same position produces no events.
    sink.clear()
    sink.handle(touchpad_report(count=1, present=1, x=65, y=32))
    assert sink.names() == []

    # Lifting releases the contact.
    sink.clear()
    sink.handle(touchpad_report(count=0, present=0, x=65, y=32))
    assert sink.names() == [("ABS_MT_TRACKING_ID", -1)]


def test_touchpad_position_is_scaled_to_the_device_range(sink: FakeEventSink) -> None:
    sink.bind(TOUCHPAD)
    # 0..127 in the descriptor, 0..2047 in the uinput device.  Y stays at zero,
    # which is where the contact already is, so it is not reported.
    sink.handle(touchpad_report(count=1, present=1, x=127, y=0))
    assert sink.names() == [
        ("ABS_MT_SLOT", 0),
        ("ABS_MT_TRACKING_ID", 1),
        ("ABS_MT_POSITION_X", 2047),
    ]


# -- robustness -------------------------------------------------------------


def test_truncated_report_is_ignored(sink: FakeEventSink) -> None:
    instance = sink.bind(BOOT_KEYBOARD)
    sink.handle(bytes([0, 0, 0]))
    assert sink.events == []
    assert any("too short" in reason for reason in instance.unsupported)


def test_empty_report_is_ignored(sink: FakeEventSink) -> None:
    instance = sink.bind(MULTI_REPORT_KEYBOARD)
    sink.handle(b"")
    assert sink.events == []
    assert sink.syncs == 0
    assert instance.unsupported == set()


def test_usages_outside_the_profile_are_reported() -> None:
    """A keyboard only profile cannot express a gamepad's buttons."""
    keyboard_only = FakeEventSink(profile="keyboard")
    instance = keyboard_only.bind(GAMEPAD)
    assert instance.unsupported
    assert any("BTN_SOUTH" in reason for reason in instance.unsupported)
    # Nothing is emitted: the keyboard profile has no buttons and no axes.
    keyboard_only.handle(gamepad_report(buttons=1, x=0x80))
    assert keyboard_only.events == []


def test_release_all_clears_held_keys(sink: FakeEventSink) -> None:
    instance = sink.bind(BOOT_KEYBOARD)
    sink.handle(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))
    assert {name for name, _ in sink.names()} == {"KEY_LEFTSHIFT", "KEY_A"}

    sink.clear()
    instance.release_all()
    assert sorted(sink.names()) == [("KEY_A", 0), ("KEY_LEFTSHIFT", 0)]


def test_release_all_clears_contacts(sink: FakeEventSink) -> None:
    instance = sink.bind(TOUCHPAD)
    sink.handle(touchpad_report(count=1, present=1, x=10, y=10))
    sink.clear()
    instance.release_all()
    assert ("ABS_MT_TRACKING_ID", -1) in sink.names()


def test_hat_state_is_cleared_on_release(sink: FakeEventSink) -> None:
    instance = sink.bind(GAMEPAD)
    sink.handle(gamepad_report(hat=1))  # north east
    sink.clear()
    instance.release_all()
    assert sorted(sink.names()) == [("BTN_DPAD_RIGHT", 0), ("BTN_DPAD_UP", 0)]

    # After the release the converter must not think the hat is still pressed.
    sink.clear()
    sink.handle(gamepad_report(hat=0))  # north
    assert sink.names() == [("BTN_DPAD_UP", 1)]
