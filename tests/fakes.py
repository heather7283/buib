"""Test doubles: an event sink and a Bluetooth controller that need no hardware."""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

from buibd.bluetooth import BluetoothError, HidDescriptor
from buibd.evdev import EV_ABS, EV_KEY, EV_REL, event_name
from buibd.hid.converter import ReportConverter
from buibd.hid.descriptor import ReportDescriptor
from buibd.profiles import capabilities


@dataclass
class FakeEventSink:
    """Stands in for :class:`buibd.uinput_device.UinputDevice`."""

    profile: str = "universal"
    events: list[tuple[tuple[int, int], int]] = field(default_factory=list)
    syncs: int = 0
    released: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        self._codes = {(c[0], c[1]) for c in capabilities(self.profile)}
        self._ranges = {
            c[1]: (c[2], c[3]) for c in capabilities(self.profile) if c[0] == EV_ABS
        }
        self._pressed: set[tuple[int, int]] = set()

    # -- EventSink --------------------------------------------------------

    def supports(self, ev_type: int, ev_code: int) -> bool:
        return (ev_type, ev_code) in self._codes

    def abs_range(self, ev_code: int) -> tuple[int, int] | None:
        return self._ranges.get(ev_code)

    def emit(self, ev_type: int, ev_code: int, value: int) -> None:
        assert not self.closed, "emitted on a closed sink"
        assert self.supports(ev_type, ev_code), (
            f"emitted {ev_type}:{ev_code}, which the device does not support"
        )
        self.events.append(((ev_type, ev_code), value))
        if ev_type == EV_KEY:
            if value:
                self._pressed.add((ev_type, ev_code))
            else:
                self._pressed.discard((ev_type, ev_code))

    def sync(self) -> None:
        assert not self.closed, "synced a closed sink"
        self.syncs += 1

    # -- UinputDevice extras ---------------------------------------------

    @property
    def capabilities(self) -> tuple[Any, ...]:
        return capabilities(self.profile)

    @property
    def name(self) -> str:
        return "fake uinput device"

    @property
    def pressed(self) -> frozenset[tuple[int, int]]:
        return frozenset(self._pressed)

    def release_all(self) -> None:
        self.released += 1
        for event in sorted(self._pressed):
            self.events.append((event, 0))
        self._pressed.clear()

    def close(self) -> None:
        self.closed = True

    # -- helpers ----------------------------------------------------------

    def bind(self, report_descriptor: bytes, **kwargs: object) -> ReportConverter:
        """Attach a converter for ``report_descriptor`` and return it."""
        self.converter = ReportConverter(
            ReportDescriptor(report_descriptor),
            self,
            **kwargs,  # type: ignore[arg-type]
        )
        return self.converter

    def handle(self, data: bytes) -> None:
        """Feed one input report to the bound converter."""
        assert self.converter is not None, "call bind() first"
        self.converter.handle_input_report(data)

    def names(self) -> list[tuple[str, int]]:
        """The emitted events as ``(name, value)``, which reads well in asserts."""
        out = []
        for (ev_type, ev_code), value in self.events:
            name = name_of(ev_type, ev_code)
            out.append((name, value))
        return out

    def clear(self) -> None:
        self.events.clear()


def name_of(ev_type: int, ev_code: int) -> str:
    """The uinput constant name of an event, e.g. ``KEY_A``."""
    return event_name((ev_type, ev_code))


class FakeLink:
    """Stands in for :class:`buibd.bluetooth.HidLink`."""

    def __init__(self, controller: FakeController, address: str) -> None:
        self.controller = controller
        self.address = address
        self.descriptor = HidDescriptor(
            report_descriptor=controller.report_descriptor,
            name=controller.device_name,
            battery_level=controller.battery_level,
        )
        self.closed = False

    async def open_channels(self) -> None:
        if self.controller.channels_fail:
            raise BluetoothError(self.controller.channels_fail)

    async def close(self) -> None:
        self.closed = True
        self.controller.link = None


class FakeController:
    """A Bluetooth controller that never touches a radio."""

    def __init__(
        self,
        report_descriptor: bytes,
        *,
        device_name: str = "Fake Keyboard",
        battery_level: int | None = 87,
        fail_with: Exception | str | None = None,
        channels_fail: str | None = None,
        paired: list[str] | None = None,
    ) -> None:
        self.report_descriptor = report_descriptor
        self.device_name = device_name
        self.battery_level = battery_level
        self.fail_with = fail_with
        self.channels_fail = channels_fail
        self.paired = list(paired or [])
        self.is_open = True
        self.last_error: str | None = None
        self.link: FakeLink | None = None
        self.closed = False
        self._report_handler: Any = None
        self._disconnect_handler: Any = None

    @property
    def local_name(self) -> str:
        return "fake controller"

    async def connect_hid(
        self, address: str, *, on_report: Any, on_disconnect: Any = None
    ) -> FakeLink:
        if self.fail_with is not None:
            raise (
                BluetoothError(self.fail_with)
                if isinstance(self.fail_with, str)
                else self.fail_with
            )
        self.link = FakeLink(self, address)
        self._report_handler = on_report
        self._disconnect_handler = on_disconnect
        await self.link.open_channels()
        return self.link

    async def paired_devices(self) -> list[str]:
        return list(self.paired)

    async def unpair(self, address: str) -> bool:
        if address not in self.paired:
            return False
        self.paired.remove(address)
        return True

    async def close(self) -> None:
        self.closed = True
        self.is_open = False

    # -- test helpers -----------------------------------------------------

    def send_report(self, data: bytes) -> None:
        """Deliver an input report as if it came off the interrupt channel."""
        assert self._report_handler is not None, "not connected"
        self._report_handler(data)

    def drop_link(self, reason: str = "the device disconnected (reason 0x13)") -> None:
        assert self._disconnect_handler is not None, "not connected"
        self._disconnect_handler(reason)


__all__ = [
    "EV_ABS",
    "EV_KEY",
    "EV_REL",
    "FakeController",
    "FakeEventSink",
    "FakeLink",
    "FakeUinputDevice",
    "FakeUinputModule",
    "name_of",
]


class FakeUinputModule(types.ModuleType):
    """Enough of the uinput module to exercise the wrapper without a kernel."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__("uinput")
        self.fail = fail
        self.devices: list[FakeUinputDevice] = []

    def Device(
        self,
        events: list[Any],
        name: str,
        bustype: int,
        vendor: int,
        product: int,
        version: int,
    ) -> FakeUinputDevice:
        if self.fail:
            raise PermissionError(13, "Permission denied")
        device = FakeUinputDevice(events, name, bustype, vendor, product, version)
        self.devices.append(device)
        return device


class FakeUinputDevice:
    def __init__(
        self,
        events: list[Any],
        name: str,
        bustype: int,
        vendor: int,
        product: int,
        version: int,
    ) -> None:
        self.events = events
        self.name = name
        self.id = (bustype, vendor, product, version)
        self.codes = {(c[0], c[1]) for c in events}
        self.written: list[tuple[tuple[int, int], int]] = []
        self.synced = 0
        self.destroyed = False

    def emit(self, event: Any, value: int, syn: bool = True) -> None:
        if event not in self.codes:
            raise OSError(22, "Invalid argument")
        self.written.append((event, value))
        if syn:
            self.synced += 1

    def syn(self) -> None:
        self.synced += 1

    def destroy(self) -> None:
        if self.destroyed:
            raise OSError(5, "already destroyed")
        self.destroyed = True
