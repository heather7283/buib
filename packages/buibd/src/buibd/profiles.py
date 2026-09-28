"""Capability profiles for the uinput device.

The kernel copies the set of enabled event codes into the input device when it
is created, and ``uinput`` offers no way to add capabilities later.  Since buibd
keeps one device open for its whole lifetime, the set of capabilities has to be
chosen when the daemon starts.  A profile bundles the events one class of HID
device needs; ``universal`` is the union of all of them and the default, so
that a single daemon can serve a keyboard today and a gamepad tomorrow.

To serve only one class, start the daemon with ``--profile keyboard`` (or
``mouse``, ``gamepad``, ``touchpad``, ``media``).  A smaller set keeps
applications from mistaking the bridge for a device it is not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from buibd.evdev import Event, event_code
from buibd.hid.usages import CONSUMER_PAGE_EVENTS, KEYBOARD_PAGE_EVENTS

__all__ = ["PROFILES", "Profile", "capabilities", "profile_names"]

#: A uinput capability specifier, as the ``uinput`` module spells them:
#: ``(type, code)`` for keys and relative axes, and ``(type, code, min, max,
#: fuzz, flat)`` for absolute axes.
Capability = tuple[int, ...]

#: Ranges chosen to match what xbox controllers and touchpads report, so that
#: libinput's acceleration curves and tap-to-click heuristics work.
_STICK = (0x0000, 0xFFFF, 0x0100, 0x0400)  # 0 .. 65535, fuzz 256, flat 4096
_TRIGGER = (0x0000, 0x00FF, 0x0002, 0x0008)  # 0 .. 255
_POSITION = (0x0000, 0x07FF, 0x0000, 0x0000)  # 0 .. 2047
_PRESSURE = (0x0000, 0x00FF, 0x0000, 0x0000)


def _event(name: str) -> Event:
    event = event_code(name)
    if event is None:  # pragma: no cover - guards against a bad table above
        raise LookupError(f"uinput has no event named {name}")
    return event


def _abs(name: str, limits: tuple[int, int, int, int]) -> Capability:
    ev_type, ev_code = _event(name)
    low, high, fuzz, flat = limits
    return (ev_type, ev_code, low, high, fuzz, flat)


def _named(names: Iterable[str]) -> tuple[Capability, ...]:
    return tuple(_event(name) for name in names)


def _union(*groups: tuple[Capability, ...]) -> tuple[Capability, ...]:
    """Merge capability groups, keeping the first range seen for each event."""
    merged: dict[Event, Capability] = {}
    for group in groups:
        for capability in group:
            ev_type, ev_code = capability[0], capability[1]
            if (ev_type, ev_code) not in merged:
                merged[(ev_type, ev_code)] = capability
    return tuple(merged.values())


_KEYBOARD: Final = _named(KEYBOARD_PAGE_EVENTS.values())
_MEDIA: Final = _union(_named(CONSUMER_PAGE_EVENTS.values()))
_MOUSE_BUTTONS: Final = _named(
    (
        "BTN_LEFT",
        "BTN_RIGHT",
        "BTN_MIDDLE",
        "BTN_SIDE",
        "BTN_EXTRA",
        "BTN_BACK",
        "BTN_FORWARD",
    )
)
_MOUSE_AXES: Final = (
    _event("REL_X"),
    _event("REL_Y"),
    _event("REL_WHEEL"),
    _event("REL_HWHEEL"),
)
_GAMEPAD_BUTTONS: Final = _named(
    (
        "BTN_SOUTH",
        "BTN_EAST",
        "BTN_NORTH",
        "BTN_WEST",
        "BTN_TL",
        "BTN_TR",
        "BTN_TL2",
        "BTN_TR2",
        "BTN_SELECT",
        "BTN_START",
        "BTN_MODE",
        "BTN_THUMBL",
        "BTN_THUMBR",
        "BTN_DPAD_UP",
        "BTN_DPAD_DOWN",
        "BTN_DPAD_LEFT",
        "BTN_DPAD_RIGHT",
    )
)
_GAMEPAD_AXES: Final = (
    _abs("ABS_X", _STICK),
    _abs("ABS_Y", _STICK),
    _abs("ABS_RX", _STICK),
    _abs("ABS_RY", _STICK),
    _abs("ABS_Z", _TRIGGER),
    _abs("ABS_RZ", _TRIGGER),
    _abs("ABS_THROTTLE", _TRIGGER),
    _abs("ABS_RUDDER", _TRIGGER),
)
_TOUCHPAD_BUTTONS: Final = _named(
    (
        "BTN_TOOL_FINGER",
        "BTN_TOOL_DOUBLETAP",
        "BTN_TOOL_TRIPLETAP",
        "BTN_TOUCH",
        "BTN_STYLUS",
        "BTN_STYLUS2",
    )
)
_TOUCHPAD_ABS: Final = (
    _abs("ABS_X", _POSITION),
    _abs("ABS_Y", _POSITION),
    _abs("ABS_MT_SLOT", (0, 9, 0, 0)),
    _abs("ABS_MT_POSITION_X", _POSITION),
    _abs("ABS_MT_POSITION_Y", _POSITION),
    _abs("ABS_MT_TRACKING_ID", (0, 65535, 0, 0)),
    _abs("ABS_MT_PRESSURE", _PRESSURE),
    _abs("ABS_MT_TOUCH_MAJOR", _PRESSURE),
    _abs("ABS_MT_WIDTH_MAJOR", _PRESSURE),
    _abs("ABS_MT_DISTANCE", _PRESSURE),
)


@dataclass(frozen=True, slots=True)
class Profile:
    """A named set of uinput capabilities."""

    name: str
    description: str
    capabilities: tuple[Capability, ...]

    def __str__(self) -> str:
        return f"{self.name} ({len(self.capabilities)} events)"


_KEYBOARD_PROFILE = _union(_KEYBOARD, _MEDIA)
_MOUSE_PROFILE = _union(_MOUSE_BUTTONS, _MOUSE_AXES)
_GAMEPAD_PROFILE = _union(_GAMEPAD_BUTTONS, _GAMEPAD_AXES)
_TOUCHPAD_PROFILE = _union(
    _MOUSE_BUTTONS, _MOUSE_AXES, _TOUCHPAD_BUTTONS, _TOUCHPAD_ABS, _GAMEPAD_BUTTONS
)

PROFILES: Final[dict[str, Profile]] = {
    profile.name: profile
    for profile in (
        Profile(
            "universal",
            "keyboard, mouse, media keys, gamepad and touchpad (default)",
            _union(
                _KEYBOARD_PROFILE,
                _MOUSE_PROFILE,
                _GAMEPAD_PROFILE,
                _TOUCHPAD_PROFILE,
            ),
        ),
        Profile("keyboard", "keyboard and multimedia keys", _KEYBOARD_PROFILE),
        Profile("mouse", "mouse buttons and relative axes", _MOUSE_PROFILE),
        Profile("gamepad", "gamepad buttons, d-pad and absolute axes", _GAMEPAD_PROFILE),
        Profile(
            "touchpad", "touchpad with multi touch, plus mouse buttons", _TOUCHPAD_PROFILE
        ),
        Profile("media", "multimedia keys only", _MEDIA),
    )
}

#: Used when ``--profile`` is not given.
DEFAULT_PROFILE: Final = "universal"


def profile_names() -> list[str]:
    return list(PROFILES)


def capabilities(profile: str) -> tuple[Capability, ...]:
    """The uinput capabilities of a profile, by name."""
    try:
        return PROFILES[profile].capabilities
    except KeyError:
        raise ValueError(
            f"unknown profile {profile!r}, expected one of {', '.join(profile_names())}"
        ) from None
