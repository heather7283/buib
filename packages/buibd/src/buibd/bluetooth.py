"""Everything that talks to the Bluetooth stack.

The rest of the daemon talks to :class:`BluetoothController` and never imports
``bumble`` itself, which keeps the session logic testable and confines the
dependency on the stack to this one module.

Two rules shape the code here:

* a failure at any point must be reported, not raised at the caller, and
* nothing in this module may close or reconfigure the uinput device.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import bumble.device
import bumble.hid
import bumble.keys
import bumble.sdp
from bumble.core import BT_HUMAN_INTERFACE_DEVICE_SERVICE, PhysicalTransport
from bumble.hci import Address
from bumble.sdp import SDP_ALL_ATTRIBUTES_RANGE, DataElement
from bumble.transport import open_transport

__all__ = [
    "BluetoothController",
    "BluetoothError",
    "HidDescriptor",
    "HidLink",
    "parse_address",
]

logger = logging.getLogger(__name__)

ReportCallback = Callable[[bytes], None]

# SDP attribute ids of a Human Interface Device service record, from the HID
# Profile 1.1 appendix.  bumble itself only knows the generic SDP attributes.
SDP_HID_SERVICE_NAME_ATTRIBUTE_ID = 0x0100
SDP_HID_SERVICE_DESCRIPTION_ATTRIBUTE_ID = 0x0101
SDP_HID_PROVIDER_NAME_ATTRIBUTE_ID = 0x0102
SDP_HID_DEVICE_RELEASE_NUMBER_ATTRIBUTE_ID = 0x0200
SDP_HID_DEVICE_SUBCLASS_ATTRIBUTE_ID = 0x0202
SDP_HID_COUNTRY_CODE_ATTRIBUTE_ID = 0x0203
SDP_HID_VIRTUAL_CABLE_ATTRIBUTE_ID = 0x0204
SDP_HID_RECONNECT_INITIATE_ATTRIBUTE_ID = 0x0205
SDP_HID_DESCRIPTOR_LIST_ATTRIBUTE_ID = 0x0206
SDP_HID_BATTERY_POWER_ATTRIBUTE_ID = 0x0209
SDP_HID_REMOTE_WAKE_ATTRIBUTE_ID = 0x020A
SDP_HID_NORMALLY_CONNECTABLE_ATTRIBUTE_ID = 0x020D
SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID = 0x020E

#: SDP attribute holding the HID descriptor list is a sequence of (type, data).
_HID_DESCRIPTOR_TYPE_REPORT: Final = 0x22

#: Fallback config used when no ``--config`` is given.  Mirrors bumble's own
#: ``classic1.json`` example, with the key store kept in the state directory.
DEFAULT_CONFIG: Final[dict[str, Any]] = {
    "name": "buib",
    "class_of_device": 0x000100,  # computer / laptop
    "keystore": "JsonKeyStore",
}

#: Seconds allowed for each stage of a connection attempt.
DEFAULT_CONNECT_TIMEOUT: Final = 30.0
_DEFAULT_AUTH_TIMEOUT: Final = 20.0


class BluetoothError(RuntimeError):
    """A Bluetooth operation failed; the caller should surface the message."""


@dataclass(frozen=True, slots=True)
class HidDescriptor:
    """What a HID device's SDP record says about itself."""

    report_descriptor: bytes
    name: str | None = None
    description: str | None = None
    provider: str | None = None
    release_number: int | None = None
    device_subclass: int | None = None
    country_code: int | None = None
    virtual_cable: bool | None = None
    reconnect_initiate: bool | None = None
    normally_connectable: bool | None = None
    remote_wake: bool | None = None
    boot_device: bool | None = None
    battery_level: int | None = None
    extra_descriptors: tuple[bytes, ...] = ()

    @property
    def display_name(self) -> str:
        return self.name or self.description or self.provider or "HID device"


@dataclass(slots=True)
class HidLink:
    """A live connection to one HID device.

    The host is shared by every link the controller opens: a
    :class:`bumble.hid.Host` registers the HID L2CAP servers (PSM 0x11 and
    0x13) when it is created, and bumble refuses to register the same PSM twice,
    so a fresh host per connection would break every reconnect.
    """

    controller: BluetoothController
    address: str
    connection: bumble.device.Connection
    host: bumble.hid.Host
    descriptor: HidDescriptor
    on_report: ReportCallback
    on_disconnect: Callable[[str], None] | None = None
    _open: bool = field(default=False, init=False)

    async def open_channels(self) -> None:
        """Connect the HID control and interrupt channels."""
        await self.host.connect_control_channel()
        await self.host.connect_interrupt_channel()
        self._open = True

    async def close(self) -> None:
        """Tear the link down, ignoring whatever has already gone away."""
        for disconnect in (
            self.host.disconnect_interrupt_channel,
            self.host.disconnect_control_channel,
        ):
            with contextlib.suppress(Exception):
                await disconnect()
        with contextlib.suppress(Exception):
            await self.connection.disconnect()
        self._open = False


def _to_python(element: bumble.sdp.DataElement) -> Any:
    """Convert an SDP data element into plain Python values."""
    if element.type in (
        bumble.sdp.DataElement.SEQUENCE,
        bumble.sdp.DataElement.ALTERNATIVE,
    ):
        return [_to_python(item) for item in element.value]
    if element.type in (
        bumble.sdp.DataElement.UNSIGNED_INTEGER,
        bumble.sdp.DataElement.SIGNED_INTEGER,
    ):
        return element.value
    if element.type == bumble.sdp.DataElement.BOOLEAN:
        return bool(element.value)
    if element.type == bumble.sdp.DataElement.UUID:
        return str(element.value)
    if isinstance(element.value, bytes):
        return element.value.decode("utf-8", "replace")
    return element.value


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, list) and value and isinstance(value[0], int):
        return value[0]
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def fetch_hid_descriptor(connection: bumble.device.Connection) -> HidDescriptor:
    """Read the HID service record of a connected device over SDP."""
    client = bumble.sdp.Client(connection)
    await client.connect()
    try:
        handles = await client.search_services([BT_HUMAN_INTERFACE_DEVICE_SERVICE])
        if not handles:
            raise BluetoothError("the peer does not offer a Bluetooth HID service")
        attributes = await client.get_attributes(handles[0], [SDP_ALL_ATTRIBUTES_RANGE])
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    values: dict[int, Any] = {}
    reports: list[bytes] = []
    for attribute in attributes:
        try:
            if attribute.id == SDP_HID_DESCRIPTOR_LIST_ATTRIBUTE_ID:
                reports = _report_descriptors(attribute.value)
                continue
            if attribute.id in values:
                continue
            values[attribute.id] = _to_python(attribute.value)
        except Exception:  # pragma: no cover - malformed element, ignore it
            logger.debug("ignoring SDP attribute 0x%04X", attribute.id, exc_info=True)

    return HidDescriptor(
        report_descriptor=reports[0] if reports else b"",
        name=_as_str(values.get(SDP_HID_SERVICE_NAME_ATTRIBUTE_ID)),
        description=_as_str(values.get(SDP_HID_SERVICE_DESCRIPTION_ATTRIBUTE_ID)),
        provider=_as_str(values.get(SDP_HID_PROVIDER_NAME_ATTRIBUTE_ID)),
        release_number=_as_int(values.get(SDP_HID_DEVICE_RELEASE_NUMBER_ATTRIBUTE_ID)),
        device_subclass=_as_int(values.get(SDP_HID_DEVICE_SUBCLASS_ATTRIBUTE_ID)),
        country_code=_as_int(values.get(SDP_HID_COUNTRY_CODE_ATTRIBUTE_ID)),
        virtual_cable=_as_bool(values.get(SDP_HID_VIRTUAL_CABLE_ATTRIBUTE_ID)),
        reconnect_initiate=_as_bool(values.get(SDP_HID_RECONNECT_INITIATE_ATTRIBUTE_ID)),
        normally_connectable=_as_bool(
            values.get(SDP_HID_NORMALLY_CONNECTABLE_ATTRIBUTE_ID)
        ),
        remote_wake=_as_bool(values.get(SDP_HID_REMOTE_WAKE_ATTRIBUTE_ID)),
        boot_device=_as_bool(values.get(SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID)),
        battery_level=_as_int(values.get(SDP_HID_BATTERY_POWER_ATTRIBUTE_ID)),
        extra_descriptors=tuple(reports[1:]),
    )


def _as_bool(value: Any) -> bool | None:
    number = _as_int(value)
    return None if number is None else bool(number)


def _report_descriptors(element: bumble.sdp.DataElement) -> list[bytes]:
    """Pull the report descriptors (type 0x22) out of a descriptor list element.

    The bytes are taken straight off the SDP element: a report descriptor looks
    like text to the SDP layer but is not text, so it must not be decoded.
    """
    found: list[bytes] = []
    entries = (
        element.value
        if element.type in (DataElement.SEQUENCE, DataElement.ALTERNATIVE)
        else ()
    )
    for entry in entries:
        items = getattr(entry, "value", None)
        if not isinstance(items, list) or len(items) != 2:
            continue
        if _as_int(_to_python(items[0])) != _HID_DESCRIPTOR_TYPE_REPORT:
            continue
        data = items[1].value
        if isinstance(data, bytes) and data:
            found.append(bytes(data))
    return found


class BluetoothController:
    """Owns the HCI transport and the bumble device the HID host runs on.

    The controller is opened lazily and survives failures: if the adapter is not
    there yet, :meth:`open` raises, the daemon keeps running, and the next
    :meth:`connect_hid` call tries again.
    """

    def __init__(
        self,
        *,
        config_path: str | os.PathLike[str] | None = None,
        transport_spec: str = "usb:0",
        state_dir: str | os.PathLike[str] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        pair: bool = False,
    ) -> None:
        self._config_path = pathlib.Path(config_path) if config_path else None
        self._transport_spec = transport_spec
        self._state_dir = pathlib.Path(state_dir) if state_dir else None
        self._connect_timeout = connect_timeout
        self._pair = pair
        self._transport: Any = None
        self._device: bumble.device.Device | None = None
        self._host: bumble.hid.Host | None = None
        self._open_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def device(self) -> bumble.device.Device:
        if self._device is None:
            raise BluetoothError("the Bluetooth controller is not open")
        return self._device

    @property
    def is_open(self) -> bool:
        return self._device is not None

    @property
    def host(self) -> bumble.hid.Host:
        """The HID host, created once and kept for every connection."""
        if self._host is None:
            if self._device is None:
                raise BluetoothError("the Bluetooth controller is not open")
            self._host = bumble.hid.Host(self._device)
        return self._host

    @property
    def local_name(self) -> str:
        return self._device.name if self._device else "buib"

    async def open(self) -> None:
        """Open the transport and power on the controller, if not already done."""
        async with self._lock:
            if self._device is not None:
                return
            if self._open_task is not None:
                # Another caller is already opening; wait for it.
                await asyncio.shield(self._open_task)
                return
            self._open_task = asyncio.current_task()
            try:
                await self._open_locked()
            finally:
                self._open_task = None

    async def _open_locked(self) -> None:
        try:
            config = self._load_config()
            logger.info("opening HCI transport %r", self._transport_spec)
            self._transport = await open_transport(self._transport_spec)
            self._device = bumble.device.Device.from_config_with_hci(
                config, self._transport.source, self._transport.sink
            )
            self._device.classic_enabled = True
            await self._device.power_on()
        except Exception as error:
            await self.close()
            self.last_error = f"{type(error).__name__}: {error}"
            raise BluetoothError(
                f"could not open the Bluetooth controller: {error}"
            ) from error
        logger.info(
            "Bluetooth controller ready: %s (%s)",
            self._device.name,
            self._device.public_address,
        )
        self.last_error = None

    async def close(self) -> None:
        """Close the transport.  The uinput device is not touched."""
        device, transport = self._device, self._transport
        self._device = self._transport = self._host = None
        if device is not None:
            with contextlib.suppress(Exception):
                await device.power_off()
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()

    def _load_config(self) -> bumble.device.DeviceConfiguration:
        if self._config_path is not None:
            return bumble.device.DeviceConfiguration.from_file(str(self._config_path))
        config = bumble.device.DeviceConfiguration()
        for key, value in DEFAULT_CONFIG.items():
            setattr(config, key, value)
        if self._state_dir is not None:
            keystore_file = self._state_dir / "keystore.json"
            keystore_file.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                keystore_file.touch(mode=0o600)
            config.keystore = f"JsonKeyStore:{keystore_file}"
        return config

    # -- pairing ----------------------------------------------------------

    async def paired_devices(self) -> list[str]:
        """Addresses the controller has link keys for."""
        await self.open()
        keystore = self.device.keystore
        if keystore is None:
            return []
        try:
            keys = await keystore.get_all()
        except Exception as error:
            raise BluetoothError(f"could not read the key store: {error}") from error
        return sorted(str(address) for address in keys)

    async def unpair(self, address: str) -> bool:
        """Drop the link key of a device.  Returns whether one was removed."""
        await self.open()
        keystore = self.device.keystore
        if keystore is None:
            return False
        try:
            await keystore.delete(address)
        except KeyError:
            return False
        except Exception as error:
            raise BluetoothError(f"could not update the key store: {error}") from error
        logger.info("removed the link key of %s", address)
        return True

    # -- connections ------------------------------------------------------

    async def connect_hid(
        self,
        address: str,
        *,
        on_report: ReportCallback,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> HidLink:
        """Connect to a HID device, set up its channels and describe it."""
        await self.open()
        device = self.device
        peer = parse_address(address)
        link: HidLink | None = None
        try:
            logger.info("connecting to %s", peer)
            connection = await _with_timeout(
                device.connect(peer, transport=PhysicalTransport.BR_EDR),
                self._connect_timeout,
                f"connecting to {peer}",
            )
            host = self.host
            link = HidLink(
                controller=self,
                address=peer,
                connection=connection,
                host=host,
                descriptor=HidDescriptor(report_descriptor=b""),
                on_report=on_report,
                on_disconnect=on_disconnect,
            )
            connection.on(
                connection.EVENT_DISCONNECTION,
                lambda reason: _on_link_lost(link, reason),
            )
            host.on(host.EVENT_INTERRUPT_DATA, _make_report_handler(link))

            await _authenticate(connection, peer, allow_pairing=self._pair)
            await _encrypt(connection)

            logger.info("querying the HID service record of %s", peer)
            descriptor = await _with_timeout(
                fetch_hid_descriptor(connection),
                self._connect_timeout,
                f"reading the HID service record of {peer}",
            )
            if not descriptor.report_descriptor:
                raise BluetoothError(
                    f"{peer} ({descriptor.display_name}) exposes no report descriptor; "
                    "boot-protocol-only devices are not supported"
                )
            link.descriptor = descriptor

            await _with_timeout(
                link.open_channels(),
                self._connect_timeout,
                f"opening the HID channels of {peer}",
            )
        except asyncio.CancelledError:
            if link is not None:
                await link.close()
            raise
        except Exception as error:
            if link is not None:
                await link.close()
            if isinstance(error, BluetoothError):
                raise
            raise BluetoothError(f"could not connect to {peer}: {error}") from error
        self.last_error = None
        return link

    async def __aenter__(self) -> BluetoothController:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


def parse_address(address: str) -> str:
    """Validate a Bluetooth address, returning it in upper case.

    The returned form is the plain ``XX:XX:XX:XX:XX:XX`` an operator expects in
    ``buibctl connect``, without the address type qualifier bumble adds.
    """
    text = address.strip()
    try:
        peer = Address.from_string_for_transport(text, transport=PhysicalTransport.BR_EDR)
    except Exception as error:
        raise BluetoothError(f"invalid Bluetooth address {address!r}: {error}") from error
    return peer.to_string(with_type_qualifier=False)


async def _with_timeout(awaitable: Awaitable[Any], timeout: float, what: str) -> Any:
    try:
        async with asyncio.timeout(timeout):
            return await awaitable
    except TimeoutError as error:
        raise BluetoothError(f"timed out after {timeout:g}s while {what}") from error


async def _authenticate(
    connection: bumble.device.Connection, peer: str, *, allow_pairing: bool
) -> None:
    """Authenticate with a bonded device, pairing first if we are allowed to."""
    try:
        await _with_timeout(
            connection.authenticate(),
            _DEFAULT_AUTH_TIMEOUT,
            f"authenticating with {peer}",
        )
    except BluetoothError:
        if not allow_pairing:
            raise BluetoothError(
                f"{peer} is not paired with this host; pair it first "
                "(for example with 'bumble-pair'), or start buibd with --pair"
            ) from None
        logger.info("pairing with %s", peer)
        await _with_timeout(
            connection.pair(), _DEFAULT_AUTH_TIMEOUT, f"pairing with {peer}"
        )
        await _with_timeout(
            connection.authenticate(),
            _DEFAULT_AUTH_TIMEOUT,
            f"authenticating with {peer}",
        )


async def _encrypt(connection: bumble.device.Connection) -> None:
    """Turn on link encryption; HIDP requires it, but do not insist."""
    try:
        await _with_timeout(
            connection.encrypt(), _DEFAULT_AUTH_TIMEOUT, "enabling encryption"
        )
    except Exception as error:
        logger.warning("could not enable link encryption, continuing anyway: %s", error)


def _make_report_handler(link: HidLink) -> Callable[[bytes], None]:
    def on_interrupt_data(pdu: bytes) -> None:
        try:
            link.on_report(_input_report(pdu))
        except Exception:  # pragma: no cover - the callback is user supplied
            logger.exception("failed to handle an HID report from %s", link.address)

    return on_interrupt_data


def _input_report(pdu: bytes) -> bytes:
    """Strip the HIDP header from an interrupt channel PDU.

    A report shorter than two bytes carries no report ID, which several devices
    send when they have nothing to report.
    """
    if len(pdu) < 2:
        return b""
    return bytes(pdu[1:])


def _on_link_lost(link: HidLink, reason: int) -> None:
    logger.info("link to %s lost (reason 0x%02X)", link.address, reason)
    if link.on_disconnect is not None:
        try:
            link.on_disconnect(f"the device disconnected (reason 0x{reason:02X})")
        except Exception:  # pragma: no cover - defensive
            logger.exception("disconnect callback failed")
