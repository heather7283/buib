"""The HID session: state machine, supervision and the uinput lifetime.

The session owns exactly one rule that matters more than everything else in
this project: **the uinput device outlives every Bluetooth session**.  Whatever
happens on the link - a failed connection, a dropped ACL, a descriptor the
converter cannot parse - is caught here, turned into a status, and cleaned up by
releasing keys.  The device itself is only ever closed by the daemon, on the way
out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from buib_protocol import SessionState

from buibd.bluetooth import BluetoothError, HidLink, parse_address
from buibd.evdev import EventSink
from buibd.hid.converter import ReportConverter
from buibd.hid.descriptor import DescriptorError, ReportDescriptor

__all__ = ["HidSession", "SessionError", "SessionStatus"]

logger = logging.getLogger(__name__)


class SessionError(Exception):
    """A session command was rejected (as opposed to failing on the link)."""


@dataclass(slots=True)
class SessionStatus:
    """A snapshot of the session, as reported over IPC."""

    state: SessionState = SessionState.DISCONNECTED
    address: str | None = None
    name: str | None = None
    error: str | None = None
    connected_at: float | None = None
    reports: int = 0
    unsupported: list[str] = field(default_factory=list)
    device: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "state": self.state.value,
            "address": self.address,
            "name": self.name,
            "error": self.error,
            "reports": self.reports,
        }
        if self.connected_at is not None:
            data["connected_since"] = round(self.connected_at, 3)
            data["connected_for"] = round(time.time() - self.connected_at, 1)
        if self.unsupported:
            data["unsupported"] = list(self.unsupported)
        if self.device:
            data["device"] = dict(self.device)
        return data


class HidSession:
    """Connects to a HID device and forwards its reports to an event sink."""

    def __init__(
        self,
        sink: EventSink,
        controller: Any,
        *,
        on_change: Callable[[], None] | None = None,
        log_traceback: bool = True,
    ) -> None:
        self._sink = sink
        self._controller = controller
        self._on_change = on_change
        #: Log the full traceback of a Bluetooth failure, not just its message.
        self._log_traceback = log_traceback
        self._status = SessionStatus()
        self._link: HidLink | None = None
        self._converter: ReportConverter | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._teardown_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- state ------------------------------------------------------------

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def busy(self) -> bool:
        return self._connect_task is not None and not self._connect_task.done()

    @property
    def address(self) -> str | None:
        return self._status.address

    def _set(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self._status, key, value)
        if self._on_change is not None:
            self._on_change()

    # -- commands ---------------------------------------------------------

    async def connect(
        self, address: str, *, wait: bool = False, timeout: float = 30.0
    ) -> None:
        """Start connecting to ``address``.

        The attempt runs in the background so that the IPC server stays
        responsive; with ``wait`` the caller is held until it finishes.
        """
        if self._closed:
            raise SessionError("the daemon is shutting down")
        if self.busy:
            raise SessionError("a connection attempt is already in progress")
        if self._status.state is SessionState.CONNECTED:
            raise SessionError(f"already connected to {self._status.address}")
        # Reject a bad address now rather than after the attempt has started.
        try:
            address = parse_address(address)
        except BluetoothError as error:
            raise SessionError(str(error)) from error
        self._set(
            state=SessionState.CONNECTING,
            address=address,
            name=None,
            error=None,
            reports=0,
            unsupported=[],
            device={},
            connected_at=None,
        )
        self._connect_task = asyncio.create_task(
            self._connect(address), name=f"buibd-connect-{address}"
        )
        if wait:
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.shield(self._connect_task)
            except TimeoutError as error:
                self._connect_task.cancel()
                raise SessionError(
                    f"connecting to {address} did not finish in time"
                ) from error
            if self._status.error is not None:
                raise SessionError(self._status.error)

    async def _connect(self, address: str) -> None:
        try:
            link = await self._controller.connect_hid(
                address,
                on_report=self._on_report,
                on_disconnect=self._on_disconnect,
            )
            converter = self._build_converter(link)
            self._link = link
            self._converter = converter
            self._set(
                state=SessionState.CONNECTED,
                name=link.descriptor.display_name,
                error=None,
                connected_at=time.time(),
                unsupported=sorted(converter.unsupported),
                device=_device_info(link),
            )
            logger.info(
                "connected to %s (%s)", link.address, link.descriptor.display_name
            )
            if converter.unsupported:
                logger.warning(
                    "%d HID usage(s) have no evdev equivalent and will be dropped: %s",
                    len(converter.unsupported),
                    ", ".join(sorted(converter.unsupported)),
                )
        except asyncio.CancelledError:
            await self._teardown()
            raise
        except (BluetoothError, DescriptorError) as error:
            # The message reaches the client; the traceback is what makes a
            # failure like a broken key store diagnosable, so it is logged too.
            logger.warning(
                "connection to %s failed: %s",
                address,
                error,
                exc_info=error if self._log_traceback else None,
            )
            await self._teardown()
            self._set(state=SessionState.DISCONNECTED, address=None, error=str(error))
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("unexpected error while connecting to %s", address)
            await self._teardown()
            self._set(
                state=SessionState.DISCONNECTED,
                address=None,
                error=f"internal error: {error}",
            )

    async def disconnect(self) -> None:
        """Drop the current link, if any.  Never fails, never closes the device."""
        await self._cancel_connect()
        if self._status.state is SessionState.DISCONNECTED and self._link is None:
            raise SessionError("not connected to any device")
        await self._teardown()
        self._set(state=SessionState.DISCONNECTED, address=None, error=None)

    async def _cancel_connect(self) -> None:
        """Stop a connection attempt that is still running, if there is one."""
        task = self._connect_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def close(self) -> None:
        """Shut the session down; the event sink is left open for its owner."""
        self._closed = True
        await self._cancel_connect()
        if self._teardown_task is not None:
            self._teardown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._teardown_task
        await self._teardown()

    # -- runtime ----------------------------------------------------------

    def _build_converter(self, link: HidLink) -> ReportConverter:
        descriptor = ReportDescriptor(link.descriptor.report_descriptor)
        return ReportConverter(descriptor, self._sink)

    def _on_report(self, data: bytes) -> None:
        """Called from the Bluetooth stack for every input report."""
        converter = self._converter
        if converter is None:
            return
        try:
            converter.handle_input_report(data)
        except Exception:
            # A single bad report must never take the daemon down.
            logger.exception(
                "failed to convert an input report from %s", self._status.address
            )
            return
        self._status.reports += 1

    def _on_disconnect(self, reason: str) -> None:
        """Called from the Bluetooth stack when the peer goes away."""
        if self._teardown_task is not None and not self._teardown_task.done():
            return
        self._teardown_task = asyncio.create_task(
            self._handle_disconnect(reason), name="buibd-disconnect"
        )

    async def _handle_disconnect(self, reason: str) -> None:
        await self._teardown()
        self._set(state=SessionState.DISCONNECTED, address=None, name=None, error=reason)
        logger.info("disconnected: %s", reason)

    async def _teardown(self) -> None:
        """Release everything the session holds, keeping the sink alive."""
        link, converter = self._link, self._converter
        self._link = self._converter = None
        if converter is not None:
            with contextlib.suppress(Exception):
                converter.release_all()
        if link is not None:
            with contextlib.suppress(Exception):
                await link.close()
        # Belt and braces: the sink tracks keys itself, so make sure nothing is
        # left held down even if the converter lost track of something.
        release_all = getattr(self._sink, "release_all", None)
        if callable(release_all):
            with contextlib.suppress(Exception):
                release_all()
        if self._status.state is not SessionState.DISCONNECTED:
            self._set(state=SessionState.DISCONNECTED, connected_at=None)
        self._status.unsupported = []
        self._status.device = {}


def _device_info(link: HidLink) -> dict[str, Any]:
    """The interesting parts of a device's SDP record, for the status command."""
    descriptor = link.descriptor
    info: dict[str, Any] = {
        "name": descriptor.display_name,
        "report_descriptor_bytes": len(descriptor.report_descriptor),
    }
    for key, value in (
        ("description", descriptor.description),
        ("provider", descriptor.provider),
        ("release_number", descriptor.release_number),
        ("subclass", descriptor.device_subclass),
        ("country_code", descriptor.country_code),
        ("battery_level", descriptor.battery_level),
        ("boot_device", descriptor.boot_device),
        ("remote_wake", descriptor.remote_wake),
        ("virtual_cable", descriptor.virtual_cable),
    ):
        if value is not None:
            info[key] = value
    return info
