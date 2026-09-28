"""Tests for the uinput capability profiles and the uinput device wrapper."""

from __future__ import annotations

import sys

import pytest
from buibd.evdev import EV_ABS, EV_KEY, EV_REL, event_code, known_event_names
from buibd.hid import usages
from buibd.profiles import DEFAULT_PROFILE, PROFILES, capabilities, profile_names
from buibd.uinput_device import UinputDevice, UinputError, describe_event
from fakes import FakeUinputModule

# -- usage tables -----------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "label"),
    [
        (usages.KEYBOARD_PAGE_EVENTS, "keyboard"),
        (usages.GAMEPAD_BUTTON_EVENTS, "gamepad buttons"),
        (usages.POINTER_BUTTON_EVENTS, "pointer buttons"),
        (usages.DESKTOP_BUTTON_EVENTS, "desktop buttons"),
        (usages.DESKTOP_CONTROL_EVENTS, "desktop controls"),
        (usages.CONSUMER_PAGE_EVENTS, "consumer"),
        (usages.DIGITIZER_EVENTS, "digitizer"),
        (usages.RELATIVE_AXES, "relative axes"),
        (usages.ABSOLUTE_AXES, "absolute axes"),
    ],
)
def test_every_mapped_usage_names_a_real_uinput_event(
    table: dict[int, str], label: str
) -> None:
    known = known_event_names()
    unknown = sorted({name for name in table.values() if name not in known})
    assert not unknown, f"{label}: {unknown} are not uinput events"


def test_keyboard_page_follows_the_usb_hid_table() -> None:
    table = usages.KEYBOARD_PAGE_EVENTS
    # The HID usages are alphabetical, not in QWERTY order.
    assert table[0x04] == "KEY_A"  # a
    assert table[0x05] == "KEY_B"  # b
    assert table[0x14] == "KEY_Q"  # q
    assert table[0x1D] == "KEY_Z"  # z
    assert table[0x1E] == "KEY_1"
    assert table[0x27] == "KEY_0"
    assert table[0x28] == "KEY_ENTER"
    assert table[0x2C] == "KEY_SPACE"
    assert table[0x39] == "KEY_CAPSLOCK"
    assert table[0x3A] == "KEY_F1"
    assert table[0x45] == "KEY_F12"
    assert table[0x59] == "KEY_KP1"
    assert table[0x64] == "KEY_102ND"
    assert table[0xE0] == "KEY_LEFTCTRL"
    assert table[0xE7] == "KEY_RIGHTMETA"


def test_buttons_mean_different_things_on_a_pointer_and_a_gamepad() -> None:
    assert usages.GAMEPAD_BUTTON_EVENTS[0x01] == "BTN_SOUTH"
    assert usages.POINTER_BUTTON_EVENTS[0x01] == "BTN_LEFT"


def test_describe_usage_is_readable() -> None:
    assert describe_usage_from_tables(0x07, 0x04).startswith("0x07:0x04")
    assert "Keyboard" in describe_usage_from_tables(0x07, 0x04)
    assert "Game Pad" in describe_usage_from_tables(0x01, 0x05)


def describe_usage_from_tables(page: int, usage: int) -> str:
    return usages.describe_usage(page, usage)


# -- profiles ---------------------------------------------------------------


def test_profile_names() -> None:
    assert set(profile_names()) == {
        "universal",
        "keyboard",
        "mouse",
        "gamepad",
        "touchpad",
        "media",
    }
    assert DEFAULT_PROFILE in PROFILES


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        capabilities("nintendo")


def test_every_profile_resolves() -> None:
    for name in profile_names():
        assert capabilities(name), f"{name} has no capabilities"


def test_profiles_have_no_duplicate_events() -> None:
    for name in profile_names():
        codes = [(c[0], c[1]) for c in capabilities(name)]
        assert len(codes) == len(set(codes)), f"{name} declares an event twice"


def test_universal_is_the_union_of_the_others() -> None:
    universal = {(c[0], c[1]) for c in capabilities("universal")}
    for name in profile_names():
        if name == "universal":
            continue
        assert {(c[0], c[1]) for c in capabilities(name)} <= universal, name


def test_keyboard_profile_has_keys_but_no_mouse() -> None:
    codes = {(c[0], c[1]) for c in capabilities("keyboard")}
    assert (EV_KEY, event_code("KEY_A")[1]) in codes
    assert (EV_KEY, event_code("KEY_MUTE")[1]) in codes
    assert (EV_REL, event_code("REL_X")[1]) not in codes
    assert not any(code[0] == EV_ABS for code in codes)


def test_mouse_profile_has_relative_axes_only() -> None:
    codes = {(c[0], c[1]) for c in capabilities("mouse")}
    assert (EV_REL, event_code("REL_X")[1]) in codes
    assert (EV_KEY, event_code("BTN_LEFT")[1]) in codes
    assert not any(code[0] == EV_ABS for code in codes)


def test_gamepad_profile_has_absolute_axes_with_ranges() -> None:
    entries = {c[1]: c for c in capabilities("gamepad") if c[0] == EV_ABS}
    assert entries
    for code, entry in entries.items():
        low, high, fuzz, flat = entry[2:]
        assert low < high, describe_event((EV_ABS, code))
        assert 0 <= fuzz < (high - low) // 2
        assert 0 <= flat < (high - low) // 2


def test_touchpad_profile_has_multi_touch_slots() -> None:
    codes = {(c[0], c[1]) for c in capabilities("touchpad")}
    for name in (
        "ABS_MT_SLOT",
        "ABS_MT_POSITION_X",
        "ABS_MT_POSITION_Y",
        "ABS_MT_TRACKING_ID",
    ):
        event = event_code(name)
        assert event is not None and event in codes, name
    slot = next(
        c for c in capabilities("touchpad") if c[1] == event_code("ABS_MT_SLOT")[1]
    )
    assert slot[2:4] == (0, 9)  # ten slots


# -- the uinput device wrapper ---------------------------------------------


def test_device_is_created_with_the_profile(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice(name="test bridge", profile="keyboard")
    try:
        (created,) = fake_uinput.devices
        assert created.name == "test bridge"
        assert created.id == (0x05, 0, 1, 1)  # BUS_BLUETOOTH
        assert len(created.events) == len(capabilities("keyboard"))
    finally:
        device.close()


def test_device_reports_its_capabilities(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice(profile="mouse")
    try:
        assert device.supports_name("REL_X")
        assert device.supports_name("BTN_LEFT")
        assert not device.supports_name("KEY_A")
        assert not device.supports_name("NOT_A_KEY")
        assert device.abs_range(event_code("REL_X")[1]) is None
    finally:
        device.close()


def test_device_tracks_and_releases_keys(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice(profile="universal")
    try:
        key_a = event_code("KEY_A")
        device.emit(*key_a, 1)
        device.emit(*event_code("KEY_B"), 1)
        device.sync()
        assert device.pressed == frozenset({key_a, event_code("KEY_B")})

        device.release_all()
        assert device.pressed == frozenset()
        (created,) = fake_uinput.devices
        assert (key_a, 0) in created.written

        # Releasing again is a no-op.
        before = len(created.written)
        device.release_all()
        assert len(created.written) == before
    finally:
        device.close()


def test_device_ignores_absent_codes(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice(profile="mouse")
    try:
        with pytest.raises(UinputError, match="cannot emit"):
            device.emit(0x04, 1, 0)  # EV_MSC is not something we produce
    finally:
        device.close()


def test_device_close_is_idempotent(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice()
    device.emit(*event_code("KEY_A"), 1)
    device.close()
    device.close()
    assert fake_uinput.devices[0].destroyed
    assert device.closed

    with pytest.raises(UinputError, match="closed"):
        device.emit(*event_code("KEY_A"), 1)


def test_closing_releases_held_keys(fake_uinput: FakeUinputModule) -> None:
    device = UinputDevice(profile="keyboard")
    device.emit(*event_code("KEY_A"), 1)
    device.close()
    (created,) = fake_uinput.devices
    assert (event_code("KEY_A"), 0) in created.written


def test_device_reports_a_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "uinput", FakeUinputModule(fail=True))
    with pytest.raises(UinputError, match="could not open /dev/uinput"):
        UinputDevice()


def test_describe_event_names_the_common_ones() -> None:
    assert describe_event(event_code("KEY_A")) == "KEY_A"
    assert describe_event(event_code("REL_X")) == "REL_X"
    assert describe_event((9, 9)) == "type 9 code 9"
