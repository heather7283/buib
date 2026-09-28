"""A parser for HID report descriptors (HID 1.11, section 6.2.2).

The parser turns a report descriptor into a tree of collections plus a list of
report fields, each carrying the bit offset, width, usages and logical range of
the data it describes.  That is everything the converter needs to slice an input
report into values without hardcoding anything about a particular device.

Only the subset of the format that can appear in a real device is implemented;
unknown items are skipped rather than rejected, because HID descriptors in the
wild are full of vendor extensions.
"""

from __future__ import annotations

import enum
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from buibd.hid.usages import describe_usage

__all__ = [
    "Collection",
    "DescriptorError",
    "Field",
    "FieldFlags",
    "Report",
    "ReportDescriptor",
    "ReportType",
    "Usage",
]

#: An implementation is allowed to declare more than one report ID.
MAX_REPORT_IDS: Final = 256

#: Guard against a bogus ``Usage Maximum`` blowing up memory.
MAX_EXPANDED_USAGES: Final = 1024

#: Tag of a long item holding an extended (four byte) usage.
LONG_ITEM_USAGE_TAG: Final = 0x08

logger = logging.getLogger(__name__)


class DescriptorError(ValueError):
    """The report descriptor is malformed."""


class ReportType(enum.IntEnum):
    """HIDP report types, as they appear in a control/interrupt PDU header."""

    INPUT = 0x01
    OUTPUT = 0x02
    FEATURE = 0x03


@dataclass(frozen=True, slots=True, order=True)
class Usage:
    """A ``(usage page, usage)`` pair."""

    page: int
    id: int

    def __str__(self) -> str:
        return describe_usage(self.page, self.id)


class FieldFlags(enum.IntFlag):
    """Main item flags of an input/output/feature item."""

    CONSTANT = 0x01
    VARIABLE = 0x02
    RELATIVE = 0x04
    WRAP = 0x08
    NONLINEAR = 0x10
    NO_PREFERRED = 0x20
    NULL_STATE = 0x40
    VOLATILE = 0x80

    @property
    def is_variable(self) -> bool:
        """Whether the item holds data, as opposed to padding or a constant.

        An "array" item (neither bit set) is an index into the usage list, which
        behaves like a variable field for our purposes, so everything that is
        not explicitly constant counts as data.
        """
        return not self & FieldFlags.CONSTANT


@dataclass(frozen=True, slots=True)
class Collection:
    """A collection, i.e. a group of fields that share a device class."""

    usage: Usage
    report_id: int
    depth: int = 0
    parent: Collection | None = None

    @property
    def is_top_level(self) -> bool:
        return self.depth == 0

    def __str__(self) -> str:
        return f"Collection({self.usage}, report={self.report_id})"


@dataclass(frozen=True, slots=True)
class Field:
    """One data item: ``count`` elements of ``size`` bits, at ``offset`` bits."""

    report_id: int
    report_type: ReportType
    offset: int
    size: int
    count: int
    usages: tuple[Usage, ...]
    logical_min: int
    logical_max: int
    flags: FieldFlags
    collection: Collection | None = None

    @property
    def bits(self) -> int:
        return self.size * self.count

    @property
    def end(self) -> int:
        return self.offset + self.bits

    @property
    def is_variable(self) -> bool:
        return self.flags.is_variable

    @property
    def is_relative(self) -> bool:
        return bool(self.flags & FieldFlags.RELATIVE)

    @property
    def is_constant(self) -> bool:
        return bool(self.flags & FieldFlags.CONSTANT)

    @property
    def is_signed(self) -> bool:
        """Whether the raw bits must be read as a two's complement number.

        Relative axes are signed by convention, and so is anything whose logical
        range starts below zero.  Absolute axes with a non-negative range (a
        gamepad stick reporting ``0..255``) stay unsigned.
        """
        if self.logical_min < 0:
            return True
        return self.is_relative and self.logical_max <= (1 << (self.size - 1)) - 1

    def __str__(self) -> str:
        return (
            f"Field(bit {self.offset}+{self.bits}, "
            f"logical {self.logical_min}..{self.logical_max}, "
            f"{len(self.usages)} usage(s))"
        )


@dataclass(slots=True)
class Report:
    """All fields sharing a report type and report ID."""

    report_id: int
    report_type: ReportType
    fields: list[Field] = field(default_factory=list)
    bit_length: int = 0

    @property
    def byte_length(self) -> int:
        return math.ceil(self.bit_length / 8)

    def __str__(self) -> str:
        return (
            f"Report(id={self.report_id}, {self.report_type.name}, {self.byte_length}B)"
        )


@dataclass(frozen=True, slots=True)
class _Item:
    kind: int  # 0: main, 1: global, 2: local
    tag: int
    size: int
    value: int


def _iter_items(data: bytes) -> Iterator[_Item]:
    """Split a descriptor into its items."""
    offset, length = 0, len(data)
    while offset < length:
        prefix = data[offset]
        offset += 1
        if prefix == 0xFE:  # long item
            if offset + 2 > length:
                raise DescriptorError("truncated long item header")
            size, tag = data[offset], data[offset + 1]
            offset += 2
            kind = 3
        else:
            size = prefix & 0x03
            kind = (prefix >> 2) & 0x03
            tag = (prefix >> 4) & 0x0F
            if kind == 3:
                raise DescriptorError(f"reserved item type at offset {offset - 1}")
        if offset + size > length:
            raise DescriptorError(f"truncated item at offset {offset - 1}")
        value = int.from_bytes(data[offset : offset + size], "little") if size else 0
        offset += size
        yield _Item(kind=kind, tag=tag, size=size, value=value)


def _sign_extend(value: int, size: int) -> int:
    """Interpret an item value as a signed integer of ``size`` bytes."""
    if size == 0 or size >= 8:
        return value
    bits = size * 8
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _resolve_logical_range(
    min_size: int, min_value: int, max_size: int, max_value: int
) -> tuple[int, int]:
    """Pick a sane interpretation of a descriptor's logical minimum/maximum.

    Descriptors are ambiguous here: logical values are signed, yet plenty of
    them encode ``0x81`` as "129" rather than "-127".  Prefer the signed reading
    and fall back to the unsigned one when it does not make sense.
    """
    signed = (_sign_extend(min_value, min_size), _sign_extend(max_value, max_size))
    if signed[0] < signed[1]:
        return signed
    if min_value < max_value:
        return min_value, max_value
    # A range that is inverted either way says nothing useful: treat the field
    # as starting at zero, which is what a relative axis means.
    return 0, max_value


class ReportDescriptor:
    """A parsed HID report descriptor."""

    def __init__(self, data: bytes) -> None:
        if not data:
            raise DescriptorError("empty report descriptor")
        self._data = bytes(data)
        self._reports: dict[tuple[ReportType, int], Report] = {}
        self._collections: list[Collection] = []
        self._report_ids: list[int] = []
        self._top_level: list[Collection] = []
        self._parse()

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def collections(self) -> list[Collection]:
        return list(self._collections)

    @property
    def top_level_collections(self) -> list[Collection]:
        return list(self._top_level)

    @property
    def report_ids(self) -> list[int]:
        """Report IDs used by the descriptor, in ascending order."""
        return list(self._report_ids)

    @property
    def uses_report_ids(self) -> bool:
        """Whether input reports are prefixed with a report ID byte."""
        return bool(self._report_ids)

    def report(self, report_type: ReportType, report_id: int) -> Report | None:
        return self._reports.get((report_type, report_id))

    def input_reports(self) -> list[Report]:
        return [r for (t, _), r in sorted(self._reports.items()) if t is ReportType.INPUT]

    def reports(self) -> list[Report]:
        return [self._reports[key] for key in sorted(self._reports)]

    # -- parsing ----------------------------------------------------------

    def _report(self, report_type: ReportType, report_id: int) -> Report:
        key = (report_type, report_id)
        if key not in self._reports:
            self._reports[key] = Report(report_id=report_id, report_type=report_type)
        return self._reports[key]

    def _parse(self) -> None:
        usage_page = 0
        min_size = min_value = 0
        max_size = max_value = 0
        report_size = 0
        report_count = 0
        report_id = 0
        offsets: dict[tuple[ReportType, int], int] = {}
        stack: list[Collection] = []
        globals_stack: list[tuple[int, int, int, int, int, int, int]] = []

        # Local item state, reset after every main item.
        usages: list[Usage] = []
        usage_min = usage_max = None
        delimiter = 0

        def local_reset() -> None:
            nonlocal usages, usage_min, usage_max
            usages = []
            usage_min = usage_max = None

        for item in _iter_items(self._data):
            if item.kind == 3:  # long item
                # The only long item the HID spec defines is the four byte
                # "extended usage"; everything else is vendor specific and
                # carries nothing we can use.
                if item.tag == LONG_ITEM_USAGE_TAG and item.size == 4:
                    usages.append(Usage(page=item.value >> 16, id=item.value & 0xFFFF))
                else:
                    logger.debug("ignoring long item, tag 0x%02X", item.tag)
                continue

            if item.kind == 1:  # global
                if item.tag == 0x0:  # usage page
                    usage_page = item.value
                elif item.tag == 0x1:  # logical minimum
                    min_size, min_value = item.size, item.value
                elif item.tag == 0x2:  # logical maximum
                    max_size, max_value = item.size, item.value
                elif item.tag == 0x7:  # report size
                    report_size = item.value
                elif item.tag == 0x8:  # report ID
                    if item.value >= MAX_REPORT_IDS:
                        raise DescriptorError(f"invalid report ID {item.value}")
                    report_id = item.value
                    if report_id not in self._report_ids:
                        self._report_ids.append(report_id)
                elif item.tag == 0x9:  # report count
                    report_count = item.value
                elif item.tag == 0xA:  # push
                    globals_stack.append(
                        (
                            usage_page,
                            min_size,
                            min_value,
                            max_size,
                            max_value,
                            report_size,
                            report_count,
                        )
                    )
                elif item.tag == 0xB:  # pop
                    if not globals_stack:
                        raise DescriptorError("pop without push")
                    (
                        usage_page,
                        min_size,
                        min_value,
                        max_size,
                        max_value,
                        report_size,
                        report_count,
                    ) = globals_stack.pop()
                # physical minimum/maximum, unit exponent and unit are ignored
                continue

            if item.kind == 2:  # local
                if delimiter:
                    if item.tag == 0xA and item.value == 0:  # delimiter end
                        delimiter -= 1
                    continue
                if item.tag == 0x0:  # usage
                    usages.append(Usage(page=usage_page, id=item.value))
                elif item.tag == 0x1:  # usage minimum
                    usage_min = item.value
                elif item.tag == 0x2:  # usage maximum
                    usage_max = item.value
                elif item.tag == 0xA and item.value == 1:  # delimiter set
                    delimiter += 1
                # designators and string indices carry no event information
                continue

            # Main items.
            if item.tag == 0xA:  # collection
                usage = usages[0] if usages else Usage(page=usage_page, id=0)
                collection = Collection(
                    usage=usage,
                    report_id=report_id,
                    depth=len(stack),
                    parent=stack[-1] if stack else None,
                )
                self._collections.append(collection)
                if collection.is_top_level:
                    self._top_level.append(collection)
                stack.append(collection)
            elif item.tag == 0xC:  # end collection
                if stack:
                    stack.pop()
            elif item.tag in (0x8, 0x9, 0xB):  # input / output / feature
                report_type = {
                    0x8: ReportType.INPUT,
                    0x9: ReportType.OUTPUT,
                    0xB: ReportType.FEATURE,
                }[item.tag]
                self._add_data_item(
                    report_type=report_type,
                    report_id=report_id,
                    usage_page=usage_page,
                    report_size=report_size,
                    report_count=report_count,
                    min_size=min_size,
                    min_value=min_value,
                    max_size=max_size,
                    max_value=max_value,
                    flags=FieldFlags(item.value & 0xFF),
                    usages=usages,
                    usage_min=usage_min,
                    usage_max=usage_max,
                    collection=stack[-1] if stack else None,
                    offsets=offsets,
                )
            local_reset()

    def _add_data_item(
        self,
        *,
        report_type: ReportType,
        report_id: int,
        usage_page: int,
        report_size: int,
        report_count: int,
        min_size: int,
        min_value: int,
        max_size: int,
        max_value: int,
        flags: FieldFlags,
        usages: list[Usage],
        usage_min: int | None,
        usage_max: int | None,
        collection: Collection | None,
        offsets: dict[tuple[ReportType, int], int],
    ) -> None:
        offset = offsets.get((report_type, report_id), 0)
        if report_size == 0 or report_count == 0:
            # An empty data item: it carries no bits at all.
            return

        element_usages: tuple[Usage, ...]
        if usage_min is not None and usage_max is not None and usage_max >= usage_min:
            # A usage range belongs to the usage page in force when it was read,
            # which is not necessarily the page of the enclosing collection.
            page = usage_page
            span = min(usage_max - usage_min + 1, MAX_EXPANDED_USAGES)
            element_usages = tuple(
                Usage(page=page, id=usage_min + i) for i in range(span)
            )
        else:
            element_usages = tuple(usages)

        # Devices commonly declare several usages for a report count of one,
        # e.g. "Usage (X), Usage (Y)" followed by "Report Count (1)"; the
        # usages then name one element each.  A larger report count still wins,
        # which is what makes the six key slots of a keyboard work.
        count = report_count
        if count == 1 and len(element_usages) > 1:
            count = len(element_usages)

        # An item with no usage is padding or a reserved constant: it is kept
        # as well, so that the field list is a faithful picture of the report.
        logical_min, logical_max = _resolve_logical_range(
            min_size, min_value, max_size, max_value
        )
        data_field = Field(
            report_id=report_id,
            report_type=report_type,
            offset=offset,
            size=report_size,
            count=count,
            usages=element_usages,
            logical_min=logical_min,
            logical_max=logical_max,
            flags=flags,
            collection=collection,
        )
        report = self._report(report_type, report_id)
        report.fields.append(data_field)
        report.bit_length = max(report.bit_length, data_field.end)
        offsets[(report_type, report_id)] = data_field.end
