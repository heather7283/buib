"""Tests for the parts of the Bluetooth layer that need no radio.

The transport, the ACL and the HID channels cannot be exercised here, but the
SDP record of a HID device can be built and parsed locally, which is where most
of the "why doesn't this keyboard work" bugs live.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
from bumble.keys import JsonKeyStore
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


# -- the key store ----------------------------------------------------------


def test_the_default_keystore_is_a_file_bumble_can_read(tmp_path: Path) -> None:
    """Regression: an empty key store file made every connection fail.

    bumble's JsonKeyStore tolerates a missing file but not an empty one, and
    reports it as "Expecting value: line 1 column 1 (char 0)" - which is what a
    daemon that creates the file up front produces on its first connect.
    """
    from buibd.bluetooth import BluetoothController

    controller = BluetoothController(state_dir=tmp_path)
    config = controller._load_config()

    assert config.keystore == f"JsonKeyStore:{tmp_path / 'keystore.json'}"
    database, key_map = asyncio.run(
        JsonKeyStore("__DEFAULT__", str(tmp_path / "keystore.json")).load()
    )
    assert key_map == {}
    assert database == {"__DEFAULT__": {}}  # bumble adds the empty namespace


def test_the_key_store_is_private(tmp_path: Path) -> None:
    """Link keys are secrets: the file must not be world readable."""
    from buibd.bluetooth import BluetoothController

    controller = BluetoothController(state_dir=tmp_path)
    controller._load_config()

    mode = (tmp_path / "keystore.json").stat().st_mode
    assert mode & 0o077 == 0, f"key store mode is {mode & 0o777:o}"


def test_an_existing_key_store_is_never_truncated(tmp_path: Path) -> None:
    from buibd.bluetooth import BluetoothController

    state = {"__DEFAULT__": {"AA:BB:CC:DD:EE:FF": {"address_type": 1}}}
    keystore = tmp_path / "keystore.json"
    keystore.write_text(json.dumps(state), encoding="utf-8")

    BluetoothController(state_dir=tmp_path)._load_config()

    assert json.loads(keystore.read_text(encoding="utf-8")) == state


def test_an_empty_key_store_is_repaired(tmp_path: Path) -> None:
    """Older versions left an empty file behind; that must not be fatal."""
    from buibd.bluetooth import BluetoothController

    keystore = tmp_path / "keystore.json"
    keystore.touch()

    BluetoothController(state_dir=tmp_path)._load_config()

    assert json.loads(keystore.read_text(encoding="utf-8")) == {}
    assert _load_keys(keystore)[1] == {}


def _load_keys(path: Path) -> tuple[dict, dict]:
    """Load a key store the way bumble does, so the file has to be valid."""
    return asyncio.run(JsonKeyStore("__DEFAULT__", str(path)).load())


def test_a_configured_keystore_is_left_alone(tmp_path: Path) -> None:
    """A configuration file that names its own key store is used verbatim."""
    from buibd.bluetooth import BluetoothController

    config_file = tmp_path / "device.json"
    config_file.write_text(
        '{"name": "test", "keystore": "JsonKeyStore:%s"}' % (tmp_path / "keys.json"),
        encoding="utf-8",
    )
    controller = BluetoothController(config_path=config_file, state_dir=tmp_path)
    config = controller._load_config()

    assert config.keystore == f"JsonKeyStore:{tmp_path / 'keys.json'}"
    assert not (tmp_path / "keystore.json").exists()
