"""Tests for the parts of the Bluetooth layer that need no radio.

The transport, the ACL and the HID channels cannot be exercised here, but the
SDP record of a HID device can be built and parsed locally, which is where most
of the "why doesn't this keyboard work" bugs live.
"""

from __future__ import annotations

import pytest
from buibd.bluetooth import (
    SDP_HID_BATTERY_POWER_ATTRIBUTE_ID,
    SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID,
    SDP_HID_COUNTRY_CODE_ATTRIBUTE_ID,
    SDP_HID_DESCRIPTOR_LIST_ATTRIBUTE_ID,
    SDP_HID_DEVICE_RELEASE_NUMBER_ATTRIBUTE_ID,
    SDP_HID_DEVICE_SUBCLASS_ATTRIBUTE_ID,
    SDP_HID_PROVIDER_NAME_ATTRIBUTE_ID,
    SDP_HID_REMOTE_WAKE_ATTRIBUTE_ID,
    SDP_HID_SERVICE_NAME_ATTRIBUTE_ID,
    SDP_HID_VIRTUAL_CABLE_ATTRIBUTE_ID,
    BluetoothError,
    _as_bool,
    _as_int,
    _as_str,
    _input_report,
    _report_descriptors,
    _to_python,
    parse_address,
)
from bumble.core import UUID
from bumble.sdp import DataElement
from descriptors import BOOT_KEYBOARD


def descriptor_list(*entries: tuple[int, bytes]) -> DataElement:
    """Build a 0x0206 style element: a sequence of (type, data) pairs."""
    return DataElement.sequence(
        [
            DataElement.sequence(
                [DataElement.unsigned_integer_8(kind), DataElement.text_string(data)]
            )
            for kind, data in entries
        ]
    )


# -- SDP elements -----------------------------------------------------------


def test_integers_and_booleans() -> None:
    assert _to_python(DataElement.unsigned_integer_8(87)) == 87
    assert _to_python(DataElement.signed_integer_8(-1)) == -1
    assert _to_python(DataElement.unsigned_integer_16(1084)) == 1084
    assert _to_python(DataElement.boolean(True)) is True
    assert _to_python(DataElement.boolean(False)) is False


def test_text_is_decoded() -> None:
    assert _to_python(DataElement.text_string(b"Logitech K380")) == "Logitech K380"
    # A report descriptor must never come through here, but junk should not
    # blow up the parser either.
    assert isinstance(_to_python(DataElement.text_string(b"\xff\xfe")), str)


def test_uuids_and_sequences() -> None:
    uuid = UUID.from_16_bits(0x1124, "HID")
    assert _to_python(DataElement.uuid(uuid)) == str(uuid)
    sequence = DataElement.sequence(
        [DataElement.unsigned_integer_8(1), DataElement.text_string(b"x")]
    )
    assert _to_python(sequence) == [1, "x"]


def test_report_descriptors_keep_their_bytes() -> None:
    """A report descriptor is not text: it must survive byte for byte."""
    payload = bytes(range(256))
    element = descriptor_list((0x22, payload), (0x23, b"config"))
    assert _report_descriptors(element) == [payload]


def test_report_descriptors_ignore_other_types() -> None:
    assert _report_descriptors(descriptor_list((0x23, b"config"))) == []
    assert _report_descriptors(descriptor_list()) == []
    assert _report_descriptors(DataElement.unsigned_integer_8(3)) == []


def test_attribute_helpers() -> None:
    assert _as_int(5) == 5
    assert _as_int(True) == 1
    assert _as_int([7, 8]) == 7
    assert _as_int("x") is None
    assert _as_str("name") == "name"
    assert _as_str("") is None
    assert _as_str(None) is None
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool(None) is None


def test_attribute_ids_match_the_hid_profile() -> None:
    assert SDP_HID_SERVICE_NAME_ATTRIBUTE_ID == 0x0100
    assert SDP_HID_DEVICE_RELEASE_NUMBER_ATTRIBUTE_ID == 0x0200
    assert SDP_HID_DEVICE_SUBCLASS_ATTRIBUTE_ID == 0x0202
    assert SDP_HID_COUNTRY_CODE_ATTRIBUTE_ID == 0x0203
    assert SDP_HID_VIRTUAL_CABLE_ATTRIBUTE_ID == 0x0204
    assert SDP_HID_DESCRIPTOR_LIST_ATTRIBUTE_ID == 0x0206
    assert SDP_HID_BATTERY_POWER_ATTRIBUTE_ID == 0x0209
    assert SDP_HID_REMOTE_WAKE_ATTRIBUTE_ID == 0x020A
    assert SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID == 0x020E
    assert SDP_HID_PROVIDER_NAME_ATTRIBUTE_ID == 0x0102


# -- addresses --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["E1:CA:72:48:C4:E8", "e1:ca:72:48:c4:e8", "  E1:CA:72:48:C4:E8  "],
)
def test_addresses_are_normalised(text: str) -> None:
    assert parse_address(text) == "E1:CA:72:48:C4:E8"


@pytest.mark.parametrize("text", ["", "nonsense", "E1:CA:72:48", "00:00:00:00:00:00:00"])
def test_bad_addresses_are_rejected(text: str) -> None:
    with pytest.raises(BluetoothError, match="invalid Bluetooth address"):
        parse_address(text)


# -- interrupt channel PDUs -------------------------------------------------


def test_the_report_id_byte_is_stripped() -> None:
    assert _input_report(bytes([0x01, 0x01, 0x04])) == b"\x01\x04"
    assert _input_report(bytes([0x0A, 0x00])) == b"\x00"


def test_empty_pdus_are_dropped() -> None:
    """Devices send a bare header when they have nothing to report."""
    assert _input_report(b"") == b""
    assert _input_report(b"\x01") == b""


def test_the_boot_keyboard_survives_a_round_trip_through_sdp() -> None:
    element = descriptor_list((0x22, BOOT_KEYBOARD))
    (report,) = _report_descriptors(element)
    from buibd.hid.descriptor import ReportDescriptor

    assert ReportDescriptor(report).input_reports()[0].byte_length == 8


# -- the whole SDP record ---------------------------------------------------


def test_a_full_hid_service_record_is_understood(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attribute of a real record must be picked up, not just the report.

    This is a regression test for a whole family of bugs: a mistyped attribute
    name only shows up when a real device is connected.
    """
    from buibd import bluetooth

    attributes = {
        SDP_HID_SERVICE_NAME_ATTRIBUTE_ID: DataElement.text_string(b"MX Keys"),
        SDP_HID_PROVIDER_NAME_ATTRIBUTE_ID: DataElement.text_string(b"Logitech"),
        SDP_HID_DEVICE_RELEASE_NUMBER_ATTRIBUTE_ID: DataElement.unsigned_integer_16(
            0x0100
        ),
        SDP_HID_DEVICE_SUBCLASS_ATTRIBUTE_ID: DataElement.unsigned_integer_8(0x03),
        SDP_HID_COUNTRY_CODE_ATTRIBUTE_ID: DataElement.unsigned_integer_8(0x00),
        SDP_HID_VIRTUAL_CABLE_ATTRIBUTE_ID: DataElement.boolean(True),
        SDP_HID_DESCRIPTOR_LIST_ATTRIBUTE_ID: descriptor_list((0x22, BOOT_KEYBOARD)),
        SDP_HID_BATTERY_POWER_ATTRIBUTE_ID: DataElement.unsigned_integer_8(64),
        SDP_HID_REMOTE_WAKE_ATTRIBUTE_ID: DataElement.boolean(True),
        SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID: DataElement.boolean(False),
    }
    service_attributes = [
        type("Attribute", (), {"id": key, "value": value})()
        for key, value in attributes.items()
    ]

    class FakeSdpClient:
        def __init__(self, connection: object) -> None:
            self.connection = connection

        async def connect(self) -> None:
            return None

        async def search_services(self, uuids: object) -> list[int]:
            return [0x00010000]

        async def get_attributes(self, handle: int, ids: object) -> list[object]:
            return service_attributes

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(bluetooth.bumble.sdp, "Client", FakeSdpClient)

    import asyncio

    descriptor = asyncio.run(bluetooth.fetch_hid_descriptor(connection=object()))  # type: ignore[arg-type]

    assert descriptor.name == "MX Keys"
    assert descriptor.provider == "Logitech"
    assert descriptor.display_name == "MX Keys"
    assert descriptor.release_number == 0x0100
    assert descriptor.device_subclass == 3
    assert descriptor.country_code == 0
    assert descriptor.virtual_cable is True
    assert descriptor.battery_level == 64
    assert descriptor.remote_wake is True
    assert descriptor.boot_device is False
    assert descriptor.report_descriptor == BOOT_KEYBOARD


def test_a_device_without_a_report_descriptor_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from buibd import bluetooth

    class FakeSdpClient:
        def __init__(self, connection: object) -> None:
            self.connection = connection

        async def connect(self) -> None:
            return None

        async def search_services(self, uuids: object) -> list[int]:
            return [0x00010000]

        async def get_attributes(self, handle: int, ids: object) -> list[object]:
            return [
                type(
                    "Attribute",
                    (),
                    {
                        "id": SDP_HID_SERVICE_NAME_ATTRIBUTE_ID,
                        "value": DataElement.text_string(b"Old"),
                    },
                )(),
                type(
                    "Attribute",
                    (),
                    {
                        "id": SDP_HID_BOOT_DEVICE_ATTRIBUTE_ID,
                        "value": DataElement.boolean(True),
                    },
                )(),
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(bluetooth.bumble.sdp, "Client", FakeSdpClient)

    import asyncio

    descriptor = asyncio.run(bluetooth.fetch_hid_descriptor(connection=object()))  # type: ignore[arg-type]
    assert descriptor.report_descriptor == b""
    assert descriptor.boot_device is True


# -- the HID host -----------------------------------------------------------


class StubDevice:
    """The parts of a bumble device that a HID host touches."""

    EVENT_CONNECTION = "connection"

    def __init__(self) -> None:
        self.psms: list[int] = []
        self.events: dict[str, object] = {}

    def create_l2cap_server(self, spec: object, handler: object) -> None:
        psm = getattr(spec, "psm", 0)
        if psm in self.psms:
            raise RuntimeError(f"PSM 0x{psm:04X} is already in use")
        self.psms.append(psm)

    def on(self, event: str, handler: object) -> None:
        self.events[event] = handler


def test_the_hid_host_is_reused_across_connections() -> None:
    """A host per connection would collide on the HID L2CAP PSMs.

    bumble refuses to register PSM 0x11 twice, so the controller keeps one host
    for its whole life - which is also what makes reconnecting work.
    """
    from buibd.bluetooth import BluetoothController

    device = StubDevice()
    controller = BluetoothController()
    controller._device = device  # type: ignore[assignment]

    first = controller.host
    second = controller.host
    assert first is second
    assert sorted(device.psms) == [0x0011, 0x0013]
