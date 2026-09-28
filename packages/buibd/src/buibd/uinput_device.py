"""The uinput device owned by the daemon.

This is the daemon's most important resource: it is created once at startup,
before anything Bluetooth related happens, and stays open for the whole
lifetime of the process.  Nothing in the Bluetooth layer is allowed to close
it - on a disconnect the session only releases the keys and buttons it was
holding down, and keeps the device registered with the kernel.
"""

from __future__ import annotations

import logging
import threading
from typing import Final

from buibd.evdev import (
    BUS_BLUETOOTH,
    EV_ABS,
    EV_KEY,
    EV_REL,
    Event,
    event_code,
    event_name,
)
from buibd.profiles import DEFAULT_PROFILE, Capability, capabilities

__all__ = ["UinputDevice", "UinputError", "describe_event"]

logger = logging.getLogger(__name__)

DEFAULT_DEVICE_NAME: Final = "buib Bluetooth HID bridge"
DEFAULT_VENDOR_ID: Final = 0x0000
DEFAULT_PRODUCT_ID: Final = 0x0001


class UinputError(RuntimeError):
    """The uinput device could not be created."""


class UinputDevice:
    """A virtual input device backed by ``/dev/uinput``.

    Implements the :class:`~buibd.evdev.EventSink` interface used by the report
    converter, and additionally remembers which keys, buttons and contacts it
    is holding down so that they can all be released at once.
    """

    def __init__(
        self,
        *,
        name: str = DEFAULT_DEVICE_NAME,
        profile: str = DEFAULT_PROFILE,
        bustype: int = BUS_BLUETOOTH,
        vendor: int = DEFAULT_VENDOR_ID,
        product: int = DEFAULT_PRODUCT_ID,
        version: int = 0x0001,
    ) -> None:
        import uinput

        self._lock = threading.Lock()
        self.name = name
        self.profile = profile
        self._capabilities: tuple[Capability, ...] = capabilities(profile)
        self._codes: set[Event] = {(c[0], c[1]) for c in self._capabilities}
        self._abs_ranges: dict[int, tuple[int, int]] = {
            c[1]: (c[2], c[3])
            for c in self._capabilities
            if c[0] == EV_ABS and len(c) > 3
        }
        self._pressed: set[Event] = set()
        self._closed = False
        try:
            self._device = uinput.Device(
                list(self._capabilities),
                name=name,
                bustype=bustype,
                vendor=vendor,
                product=product,
                version=version,
            )
        except OSError as error:
            raise UinputError(_explain(error)) from error
        except Exception as error:  # pragma: no cover - defensive
            raise UinputError(f"could not create the uinput device: {error}") from error
        logger.info(
            "uinput device %r created: profile=%s, %d capabilities",
            name,
            profile,
            len(self._capabilities),
        )

    # -- introspection ----------------------------------------------------

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities

    @property
    def pressed(self) -> frozenset[Event]:
        """Events currently held down, as ``(ev_type, ev_code)`` pairs."""
        with self._lock:
            return frozenset(self._pressed)

    @property
    def closed(self) -> bool:
        return self._closed

    def supports(self, ev_type: int, ev_code: int) -> bool:
        return (ev_type, ev_code) in self._codes

    def supports_name(self, name: str) -> bool:
        event = event_code(name)
        return event is not None and self.supports(*event)

    def abs_range(self, ev_code: int) -> tuple[int, int] | None:
        return self._abs_ranges.get(ev_code)

    # -- event output -----------------------------------------------------

    def emit(self, ev_type: int, ev_code: int, value: int) -> None:
        """Queue one event.  Call :meth:`sync` to deliver it."""
        if self._closed:
            raise UinputError("the uinput device is closed")
        if ev_type not in (EV_KEY, EV_REL, EV_ABS):
            raise UinputError(f"cannot emit event type {ev_type}")
        with self._lock:
            if ev_type == EV_KEY:
                if value:
                    self._pressed.add((ev_type, ev_code))
                else:
                    self._pressed.discard((ev_type, ev_code))
            self._device.emit((ev_type, ev_code), value, syn=False)

    def sync(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._device.syn()

    def release_all(self) -> None:
        """Release every key and button that is still held down."""
        with self._lock:
            if self._closed or not self._pressed:
                return
            for ev_type, ev_code in sorted(self._pressed):
                logger.debug("releasing %s", describe_event((ev_type, ev_code)))
                self._device.emit((ev_type, ev_code), 0, syn=False)
            self._pressed.clear()
            self._device.syn()

    # -- lifetime ---------------------------------------------------------

    def close(self) -> None:
        """Destroy the device.  Only ever called while the daemon exits."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._release_all_locked()
                self._device.destroy()
            except Exception:  # pragma: no cover - best effort while exiting
                logger.exception("failed to destroy the uinput device cleanly")
            else:
                logger.info("uinput device %r destroyed", self.name)

    def _release_all_locked(self) -> None:
        for ev_type, ev_code in sorted(self._pressed):
            self._device.emit((ev_type, ev_code), 0, syn=False)
        self._pressed.clear()
        self._device.syn()

    def __enter__(self) -> UinputDevice:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _explain(error: OSError) -> str:
    hint = ""
    if error.errno in (13, 1):  # EACCES, EPERM
        hint = " (is the uinput module loaded, and is this user in the 'input' group?)"
    elif error.errno == 2:  # ENOENT
        hint = " (the uinput module does not seem to be loaded: try 'modprobe uinput')"
    return f"could not open /dev/uinput: {error}{hint}"


def describe_event(event: Event) -> str:
    """Name an event the way uinput does, e.g. ``KEY_A`` or ``REL_X``."""
    return event_name(event)
