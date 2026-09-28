"""Tests for the HID report descriptor parser."""

from __future__ import annotations

import pytest
from buibd.hid.descriptor import (
    Collection,
    DescriptorError,
    ReportDescriptor,
    ReportType,
    Usage,
)
from buibd.hid.usages import BUTTON_PAGE, GENERIC_DESKTOP_PAGE, KEYBOARD_PAGE
from descriptors import (
    BOOT_KEYBOARD,
    GAMEPAD,
    MOUSE,
    MULTI_REPORT_KEYBOARD,
    MULTIMEDIA_KEYBOARD,
    TOUCHPAD,
)


def test_empty_descriptor_is_rejected() -> None:
    with pytest.raises(DescriptorError):
        ReportDescriptor(b"")


def test_truncated_item_is_rejected() -> None:
    with pytest.raises(DescriptorError, match="truncated"):
        ReportDescriptor(bytes.fromhex("05 01 09"))  # usage item with no data


def test_reserved_item_type_is_rejected() -> None:
    with pytest.raises(DescriptorError, match="reserved"):
        ReportDescriptor(bytes.fromhex("0c 01"))  # type 3, size 0, tag 0


def test_boot_keyboard_layout() -> None:
    descriptor = ReportDescriptor(BOOT_KEYBOARD)

    assert not descriptor.uses_report_ids
    assert descriptor.report_ids == []
    (report,) = descriptor.input_reports()
    assert report.report_id == 0
    assert report.byte_length == 8

    top = descriptor.top_level_collections
    assert [c.usage for c in top] == [Usage(GENERIC_DESKTOP_PAGE, 0x06)]
    assert top[0].is_top_level

    # 8 modifier bits, 8 reserved bits, 6 key slots.  The LED and padding
    # items in between are output items, so they live in another report.
    modifiers, reserved, keys = report.fields
    assert (modifiers.offset, modifiers.size, modifiers.count) == (0, 1, 8)
    assert modifiers.is_variable and not modifiers.is_relative
    assert modifiers.usages[0] == Usage(KEYBOARD_PAGE, 0xE0)
    assert modifiers.usages[-1] == Usage(KEYBOARD_PAGE, 0xE7)
    assert modifiers.logical_min == 0 and modifiers.logical_max == 1

    assert (reserved.offset, reserved.size, reserved.count) == (8, 8, 1)
    assert reserved.usages == ()

    assert (keys.offset, keys.size, keys.count) == (16, 8, 6)
    assert keys.usages[0] == Usage(KEYBOARD_PAGE, 0x00)
    assert keys.usages[-1] == Usage(KEYBOARD_PAGE, 0x65)
    assert (keys.logical_min, keys.logical_max) == (0, 101)


def test_mouse_layout() -> None:
    descriptor = ReportDescriptor(MOUSE)
    (report,) = descriptor.input_reports()
    assert report.byte_length == 4

    buttons, padding, xy, wheel = report.fields
    assert (buttons.offset, buttons.size, buttons.count) == (0, 1, 3)
    assert buttons.usages == tuple(Usage(BUTTON_PAGE, i) for i in (1, 2, 3))
    assert (padding.offset, padding.size, padding.count) == (3, 5, 1)
    assert padding.usages == ()

    # X and Y share one item: two usages declared for a report count of one.
    assert (xy.offset, xy.size, xy.count) == (8, 8, 2)
    assert xy.usages == (
        Usage(GENERIC_DESKTOP_PAGE, 0x30),
        Usage(GENERIC_DESKTOP_PAGE, 0x31),
    )
    assert (wheel.offset, wheel.size, wheel.count) == (24, 8, 1)

    for axis in (xy, wheel):
        assert (axis.logical_min, axis.logical_max) == (-127, 127)
        assert axis.is_relative and axis.is_signed


def test_gamepad_layout() -> None:
    descriptor = ReportDescriptor(GAMEPAD)
    assert descriptor.uses_report_ids
    assert descriptor.report_ids == [1]

    (report,) = descriptor.input_reports()
    assert report.report_id == 1
    assert report.byte_length == 9

    assert [c.usage for c in descriptor.top_level_collections] == [
        Usage(GENERIC_DESKTOP_PAGE, 0x05)
    ]
    assert [c.usage for c in descriptor.collections] == [
        Usage(GENERIC_DESKTOP_PAGE, 0x05),  # Game Pad
        Usage(GENERIC_DESKTOP_PAGE, 0x01),  # Pointer
    ]

    buttons, _padding, hat, _hat_padding = report.fields[:4]
    assert (buttons.offset, buttons.size, buttons.count) == (0, 1, 10)
    assert buttons.usages[0] == Usage(BUTTON_PAGE, 0x01)
    assert (hat.offset, hat.size) == (12, 4)
    assert (hat.logical_min, hat.logical_max) == (0, 7)

    xy, zr = report.fields[4:]
    assert (xy.offset, xy.size, xy.count) == (20, 16, 2)
    assert [u.id for u in xy.usages] == [0x30, 0x31]
    assert (zr.offset, zr.size, zr.count) == (52, 8, 2)
    for axes in (xy, zr):
        assert (axes.logical_min, axes.logical_max) == (0, 255)
        assert not axes.is_signed
        assert not axes.is_relative
        # The axes live in a Pointer collection nested in a Game Pad one.
        assert axes.collection is not None
        assert axes.collection.usage == Usage(GENERIC_DESKTOP_PAGE, 0x01)
        assert axes.collection.parent is not None
        assert axes.collection.parent.usage == Usage(GENERIC_DESKTOP_PAGE, 0x05)


def test_touchpad_layout() -> None:
    descriptor = ReportDescriptor(TOUCHPAD)
    (report,) = descriptor.input_reports()
    fields = {field.usages[0].id: field for field in report.fields if field.usages}
    # Contact count, padding, finger present, X and Y.
    assert fields[0x01].count == 5
    assert fields[0x22].count == 1
    assert (fields[0x30].offset, fields[0x30].count) == (7, 2)
    assert fields[0x30].usages[1].id == 0x31
    assert report.byte_length == 3

    finger = descriptor.collections[1]
    assert finger.usage == Usage(0x0D, 0x03)
    assert finger.depth == 1


def test_multiple_report_ids_are_kept_apart() -> None:
    descriptor = ReportDescriptor(MULTI_REPORT_KEYBOARD)
    assert descriptor.report_ids == [1, 2]

    first = descriptor.report(ReportType.INPUT, 1)
    second = descriptor.report(ReportType.INPUT, 2)
    assert first is not None and second is not None
    assert [f.offset for f in first.fields] == [0, 8]
    assert [f.offset for f in second.fields] == [0]
    assert second.fields[0].usages[0] == Usage(KEYBOARD_PAGE, 0x3A)
    assert descriptor.report(ReportType.INPUT, 3) is None


def test_output_items_do_not_shift_input_offsets() -> None:
    descriptor = ReportDescriptor(BOOT_KEYBOARD)
    report = descriptor.report(ReportType.OUTPUT, 0)
    assert report is not None
    # The five LED bits and the three padding bits of the boot keyboard.
    assert [(f.offset, f.size) for f in report.fields] == [(0, 1), (5, 3)]
    assert report.fields[0].usages[0].page == 0x08  # LED page
    # The input keys still start right after the eight reserved bits.
    assert descriptor.input_reports()[0].fields[2].offset == 16


def test_usage_minimum_expands_to_a_range() -> None:
    # Usage (Keyboard), Usage Minimum (4), Usage Maximum (8), 5 x 1 bit.
    data = bytes.fromhex("05 07 19 04 29 08 75 01 95 05 81 02")
    (report,) = ReportDescriptor(data).input_reports()
    (keys,) = report.fields
    assert [u.id for u in keys.usages] == [4, 5, 6, 7, 8]
    assert all(u.page == KEYBOARD_PAGE for u in keys.usages)


def test_extended_usage_selects_its_own_page() -> None:
    # Usage Page (Keyboard) then a long item usage with page 0x0C up top.
    data = bytes.fromhex("05 07fe 04 08 01 00 0c 0075 01 95 01 81 02")
    (report,) = ReportDescriptor(data).input_reports()
    (field,) = report.fields
    assert field.usages == (Usage(0x0C, 0x01),)


def test_push_and_pop_restore_global_state() -> None:
    data = bytes.fromhex(
        "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"  # 8 modifier bits
        "a4"  # push
        "75 08 95 06 15 00 25 65 81 00"  # 6 byte sized keys
        "b4"  # pop: back to 1 bit sized
        "95 01 81 02"  # 1 more bit, size 1 again
    )
    (report,) = ReportDescriptor(data).input_reports()
    assert [(f.offset, f.size, f.count) for f in report.fields] == [
        (0, 1, 8),
        (8, 8, 6),
        (56, 1, 1),
    ]


def test_delimiter_skips_nested_local_items() -> None:
    # A usage inside a delimiter set does not belong to the field.
    data = bytes.fromhex("05 07a9 0119 04a9 0009 0575 01 95 01 81 02")
    (report,) = ReportDescriptor(data).input_reports()
    (field,) = report.fields
    assert field.usages == (Usage(KEYBOARD_PAGE, 0x05),)


def test_unbalanced_pop_is_rejected() -> None:
    with pytest.raises(DescriptorError, match="pop without push"):
        ReportDescriptor(bytes.fromhex("b4 05 07 75 01 95 01 81 02"))


def test_inconsistent_logical_range_falls_back_to_unsigned() -> None:
    # Logical minimum 0x81 and maximum 0x81: signed would give -127..-127.
    data = bytes.fromhex("05 01 09 30 15 81 25 81 75 08 95 01 81 02")
    (report,) = ReportDescriptor(data).input_reports()
    (x,) = report.fields
    assert (x.logical_min, x.logical_max) == (0, 129)


def test_multi_media_keyboard_reports_the_consumer_page() -> None:
    descriptor = ReportDescriptor(MULTIMEDIA_KEYBOARD)
    (report,) = descriptor.input_reports()
    consumer = [f for f in report.fields if f.usages and f.usages[0].page == 0x0C]
    assert len(consumer) == 1
    assert consumer[0].count == 3
    assert [u.id for u in consumer[0].usages] == [0x0A, 0x23, 0xB0]


def test_collections_keep_their_parent() -> None:
    descriptor = ReportDescriptor(GAMEPAD)
    gamepad, pointer = descriptor.collections
    assert isinstance(gamepad, Collection)
    assert gamepad.parent is None
    assert pointer.parent is gamepad
    assert pointer.depth == 1
