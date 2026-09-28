"""Thin indirection over the ``uinput`` extension module.

Only this module imports :mod:`uinput`; everything else in the daemon deals in
plain ``(ev_type, ev_code)`` tuples and symbolic names such as ``"KEY_A"``.  The
indirection keeps the HID code testable without a writable ``/dev/uinput`` and
lets the daemon be exercised with a fake sink.
"""

from __future__ import annotations

import functools
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "BUS_BLUETOOTH",
    "EV_ABS",
    "EV_KEY",
    "EV_MSC",
    "EV_REL",
    "EV_SYN",
    "Event",
    "EventSink",
    "event_code",
    "event_name",
    "known_event_names",
]

#: Event types. ``uinput`` does not export these, so we spell them out.
EV_SYN: Final = 0x00
EV_KEY: Final = 0x01
EV_REL: Final = 0x02
EV_ABS: Final = 0x03
EV_MSC: Final = 0x04

#: ``BUS_BLUETOOTH`` from ``linux/input.h``; makes the device show up as a
#: Bluetooth device in ``/proc/bus/input/devices``.
BUS_BLUETOOTH: Final = 0x05

#: An ``(ev_type, ev_code)`` pair, matching the tuples ``uinput`` passes around.
Event = tuple[int, int]

_EVENT_TYPES: Final = (EV_KEY, EV_REL, EV_ABS, EV_MSC)


@runtime_checkable
class EventSink(Protocol):
    """Minimal interface the report converter needs from the uinput device."""

    def supports(self, ev_type: int, ev_code: int) -> bool:
        """Whether the device was created with this capability enabled."""
        ...

    def abs_range(self, ev_code: int) -> tuple[int, int] | None:
        """The ``(min, max)`` range the device declared for an ``EV_ABS`` code."""
        ...

    def emit(self, ev_type: int, ev_code: int, value: int) -> None:
        """Queue one event; it reaches userspace on the next :meth:`sync`."""
        ...

    def sync(self) -> None:
        """Flush every queued event to userspace as one atomic group."""
        ...


@functools.cache
def _codes() -> dict[str, Event]:
    import uinput

    codes: dict[str, Event] = {}
    for name in dir(uinput):
        if not name.startswith(("KEY_", "BTN_", "REL_", "ABS_")):
            continue
        value = getattr(uinput, name)
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and value[0] in _EVENT_TYPES
            and isinstance(value[1], int)
        ):
            codes.setdefault(name, (int(value[0]), int(value[1])))
    return codes


def known_event_names() -> frozenset[str]:
    """Names of every ``uinput`` event constant, e.g. ``{"KEY_A", "ABS_X", ...}``."""
    return frozenset(_codes())


def event_code(name: str) -> Event | None:
    """Resolve ``"KEY_A"`` to ``(EV_KEY, 30)``, or ``None`` if unknown."""
    return _codes().get(name)


@functools.cache
def _names() -> dict[Event, str]:
    """Map every event code back to a name, for log and status messages.

    ``uinput`` has aliases - ``BTN_SOUTH`` and ``BTN_A`` are the same code - so
    the names buib itself can produce are preferred over the alternatives.
    """
    from buibd.hid import usages

    produced: set[str] = set()
    for table in (
        usages.KEYBOARD_PAGE_EVENTS,
        usages.GAMEPAD_BUTTON_EVENTS,
        usages.POINTER_BUTTON_EVENTS,
        usages.DESKTOP_BUTTON_EVENTS,
        usages.DESKTOP_CONTROL_EVENTS,
        usages.CONSUMER_PAGE_EVENTS,
        usages.DIGITIZER_EVENTS,
        usages.RELATIVE_AXES,
        usages.ABSOLUTE_AXES,
    ):
        produced.update(table.values())
    produced.update(
        {
            "BTN_DPAD_UP",
            "BTN_DPAD_DOWN",
            "BTN_DPAD_LEFT",
            "BTN_DPAD_RIGHT",
            "BTN_TRIGGER",
            "ABS_MT_SLOT",
            "ABS_MT_TRACKING_ID",
        }
    )
    names: dict[Event, str] = {}
    for name, event in sorted(_codes().items()):
        if event not in names or (name in produced and names[event] not in produced):
            names[event] = name
    return names


def event_name(event: Event) -> str:
    """The name of an event, e.g. ``KEY_A``; a placeholder if there is none."""
    return _names().get(event, f"type {event[0]} code {event[1]}")
