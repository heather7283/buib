"""Translate HID input reports into evdev events.

The converter binds every data item of every input report to an evdev event
once, when a device connects, and afterwards only has to slice bits out of each
incoming report.  Usages that have no evdev equivalent, or that the daemon's
uinput device was not created with, are collected in
:attr:`ReportConverter.unsupported` so that the session can tell the user
instead of silently dropping input.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from buibd.evdev import EV_ABS, Event, EventSink, event_code
from buibd.hid.descriptor import Field, Report, ReportDescriptor, ReportType, Usage
from buibd.hid.usages import (
    ABSOLUTE_AXES,
    ABSOLUTE_COLLECTIONS,
    BUTTON_PAGE,
    CONSUMER_PAGE,
    DESKTOP_BUTTON_EVENTS,
    DESKTOP_CONTROL_EVENTS,
    DIGITIZER_EVENTS,
    DIGITIZER_FINGER_USAGES,
    DIGITIZER_PAGE,
    DIGITIZER_TOUCH_USAGES,
    GAMEPAD_BUTTON_EVENTS,
    GENERIC_DESKTOP_PAGE,
    KEYBOARD_PAGE,
    KEYBOARD_PAGE_EVENTS,
    MULTI_TOUCH_COLLECTIONS,
    POINTER_BUTTON_EVENTS,
    POINTER_COLLECTIONS,
    RELATIVE_AXES,
    consumer_event,
    describe_usage,
    generic_desktop_event,
)

__all__ = ["Binding", "BindingKind", "ReportConverter", "read_bits"]

logger = logging.getLogger(__name__)

#: Digitizer axes of a multi touch device, and the evdev axes they feed.
_MT_AXIS_EVENTS: Final[dict[int, tuple[str, int]]] = {
    0x30: ("ABS_MT_POSITION_X", 0),
    0x31: ("ABS_MT_POSITION_Y", 1),
    0x32: ("ABS_MT_TOUCH_MAJOR", 2),  # Z of a finger is its size
}

#: The kernel's limit on the number of simultaneous contacts, and the absolute
#: codes the converter needs for multi touch bookkeeping.
MAX_SLOTS = 10

# Absolute codes used for multi touch bookkeeping.  ``uinput`` spells them out,
# but the converter only needs the numbers.
_ABS_MT_SLOT = 0x2F
_ABS_MT_TRACKING_ID = 0x39

#: Directions of an 8 way hat switch, indexed by the hat's value (clockwise).
_HAT_CW: dict[int, tuple[str, ...]] = {
    0: ("BTN_DPAD_UP",),
    1: ("BTN_DPAD_UP", "BTN_DPAD_RIGHT"),
    2: ("BTN_DPAD_RIGHT",),
    3: ("BTN_DPAD_DOWN", "BTN_DPAD_RIGHT"),
    4: ("BTN_DPAD_DOWN",),
    5: ("BTN_DPAD_DOWN", "BTN_DPAD_LEFT"),
    6: ("BTN_DPAD_LEFT",),
    7: ("BTN_DPAD_UP", "BTN_DPAD_LEFT"),
    8: (),  # the null state of a nine position hat
}

#: The same, for hat switches that count counter-clockwise.
_HAT_CCW: dict[int, tuple[str, ...]] = {
    0: ("BTN_DPAD_UP",),
    1: ("BTN_DPAD_UP", "BTN_DPAD_LEFT"),
    2: ("BTN_DPAD_LEFT",),
    3: ("BTN_DPAD_DOWN", "BTN_DPAD_LEFT"),
    4: ("BTN_DPAD_DOWN",),
    5: ("BTN_DPAD_DOWN", "BTN_DPAD_RIGHT"),
    6: ("BTN_DPAD_RIGHT",),
    7: ("BTN_DPAD_UP", "BTN_DPAD_RIGHT"),
    8: (),
}


class BindingKind(enum.Enum):
    """What a bound field means, and therefore how its value becomes events."""

    KEY = "key"  # 0 or non-zero: pressed or released
    INDEX = "index"  # a usage index, e.g. the six keys of a keyboard array
    REL = "rel"  # a relative axis
    ABS = "abs"  # an absolute axis
    HAT = "hat"  # a compass style switch
    TOUCH = "touch"  # a contact is in range
    MT_AXIS = "mt_axis"  # the position of a contact


@dataclass(slots=True)
class Binding:
    """One data item (or one element of it) bound to an evdev event."""

    kind: BindingKind
    report_id: int
    offset: int
    size: int
    signed: bool
    usage: Usage | None
    field: Field
    event: Event | None = None
    event_name: str | None = None
    #: Position of this element inside its field.
    index: int = 0
    #: For multi touch axes: 0 = X, 1 = Y, 2 = Z.
    axis: int = 0
    #: Which contact of a multi touch device this field belongs to.
    finger: int = 0
    #: Whether the device is a pointer, which changes what a button means.
    pointer: bool = False
    #: For an array field: what to subtract from the value to index the usages.
    array_base: int = 1
    #: Last value emitted, so that only changes reach userspace.
    value: int = 0

    def __str__(self) -> str:
        target = self.event_name or self.kind.value
        return f"{target}@{self.offset}+{self.size}"


def read_bits(data: bytes, offset: int, size: int, *, signed: bool = False) -> int:
    """Read ``size`` bits starting at bit ``offset`` (LSB first, HID layout)."""
    if size == 0:
        return 0
    value = 0
    for index in range(size):
        bit = offset + index
        byte = bit >> 3
        if byte >= len(data):
            raise IndexError(f"need {byte + 1} bytes, got {len(data)}")
        value |= ((data[byte] >> (bit & 7)) & 1) << index
    if signed and value & (1 << (size - 1)):
        value -= 1 << size
    return value


def _ancestors(data_field: Field) -> Iterable[tuple[int, int]]:
    collection = data_field.collection
    while collection is not None:
        yield collection.usage.page, collection.usage.id
        collection = collection.parent


def _finger_index(descriptor: ReportDescriptor) -> dict[int, int]:
    """Number the contact collections of a descriptor.

    A touchpad describes one "Finger" collection per contact; their order in the
    descriptor is what turns their fields into evdev slots.
    """
    counters: dict[int, int] = {}
    fingers: dict[int, int] = {}
    for collection in descriptor.collections:
        usage = collection.usage
        if usage.page != DIGITIZER_PAGE or usage.id not in DIGITIZER_FINGER_USAGES:
            continue
        parent = id(collection.parent) if collection.parent is not None else 0
        index = counters.get(parent, 0)
        counters[parent] = index + 1
        if index < MAX_SLOTS:
            fingers[id(collection)] = index
    return fingers


def _array_base(usages: tuple[Usage, ...]) -> int:
    """How an array field's values map onto its usage list.

    The HID specification says the value is a one based index, but the boot
    keyboard (and most of the devices that copy it) declares
    ``Usage Minimum (0), Usage Maximum (101)`` and then sends the usage id
    itself: 0x04 for "a".  Both conventions are therefore supported, by looking
    at whether the list is the contiguous run that starts at zero.
    """
    if usages and all(usage.id == index for index, usage in enumerate(usages)):
        return 0
    return 1


def _is_key_usage(usage: Usage) -> bool:
    """Whether a usage names a key or a button rather than an axis."""
    if usage.page in (KEYBOARD_PAGE, BUTTON_PAGE, CONSUMER_PAGE):
        return True
    if usage.page == GENERIC_DESKTOP_PAGE:
        return usage.id in DESKTOP_BUTTON_EVENTS or usage.id in DESKTOP_CONTROL_EVENTS
    return False


class _MultiTouch:
    """Contact bookkeeping for multi touch digitizers (evdev protocol B).

    Each contact the descriptor describes - a "Finger" collection, or a single
    implicit contact on a device that only reports a contact count - owns one
    slot, and the converter tells this class which finger each report belongs
    to.
    """

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        self._contacts: dict[int, int] = {}  # finger -> tracking id
        self._slot: int | None = None
        self._next_id = 1
        self._persistent = False  # the device never reports contact state
        self._announced = False

    @property
    def persistent(self) -> bool:
        return self._persistent

    def set_persistent(self) -> None:
        """Use a single permanent contact, for devices without a touch flag."""
        self._persistent = True

    def _select(self, slot: int) -> bool:
        """Make a slot current, returning whether that was a change."""
        if self._slot == slot:
            return False
        self._sink.emit(EV_ABS, _ABS_MT_SLOT, slot)
        self._slot = slot
        return True

    def is_down(self, finger: int) -> bool:
        return self._persistent or finger in self._contacts

    def select(self, finger: int) -> bool:
        """Make a finger the current slot, announcing it if it is the first."""
        if self._persistent:
            if self._announced:
                return False
            self._select(0)
            self._sink.emit(EV_ABS, _ABS_MT_TRACKING_ID, 0)
            self._announced = True
            return True
        if finger not in self._contacts:
            return False
        return self._select(finger)

    def down(self, finger: int) -> None:
        if self._persistent or finger in self._contacts:
            return
        self._contacts[finger] = self._next_id
        self._next_id = self._next_id % 32766 + 1
        self._select(finger)
        self._sink.emit(EV_ABS, _ABS_MT_TRACKING_ID, self._contacts[finger])

    def up(self, finger: int) -> None:
        if self._persistent or finger not in self._contacts:
            return
        self._select(finger)
        del self._contacts[finger]
        self._sink.emit(EV_ABS, _ABS_MT_TRACKING_ID, -1)

    def release_all(self) -> None:
        if self._announced:
            self._select(0)
            self._sink.emit(EV_ABS, _ABS_MT_TRACKING_ID, -1)
            self._announced = False
        for finger in sorted(self._contacts, reverse=True):
            self._select(finger)
            self._sink.emit(EV_ABS, _ABS_MT_TRACKING_ID, -1)
        self._contacts.clear()


class ReportConverter:
    """Turns HID input reports into evdev events on an :class:`EventSink`."""

    def __init__(
        self,
        descriptor: ReportDescriptor,
        sink: EventSink,
        *,
        hat_direction: str = "cw",
    ) -> None:
        if hat_direction not in ("cw", "ccw"):
            raise ValueError("hat_direction must be 'cw' or 'ccw'")
        self._descriptor = descriptor
        self._sink = sink
        self._hat_table = _HAT_CW if hat_direction == "cw" else _HAT_CCW
        self._fingers = _finger_index(descriptor)
        self._mt = _MultiTouch(sink)
        self.unsupported: set[str] = set()
        self._layout: dict[int, list[Binding]] = {
            report.report_id: self._bind_report(report)
            for report in descriptor.input_reports()
        }
        self._index_state: dict[tuple[int, int], int] = {}
        self._hat_state: dict[tuple[int, int], tuple[str, ...]] = {}
        self._down: set[int] = set()
        self._reset()

    @property
    def bindings(self) -> list[Binding]:
        """Every binding, in report order (mostly useful for tests and logging)."""
        return [b for bindings in self._layout.values() for b in bindings]

    def __str__(self) -> str:
        return f"ReportConverter({len(self.bindings)} bindings)"

    # -- binding ----------------------------------------------------------

    def _bind_report(self, report: Report) -> list[Binding]:
        bindings = [
            b for f in report.fields for b in self._bind_field(f) if b is not None
        ]
        if not any(b.kind is BindingKind.TOUCH for b in bindings) and any(
            b.kind is BindingKind.MT_AXIS for b in bindings
        ):
            # A device that never reports contact state has one permanent finger.
            self._mt.set_persistent()
        return bindings

    def _finger_of(self, data_field: Field) -> int:
        """Which contact of a multi touch device a field belongs to."""
        collection = data_field.collection
        return self._fingers.get(id(collection), 0) if collection is not None else 0

    def _bind_field(self, data_field: Field) -> list[Binding | None]:
        if data_field.report_type is not ReportType.INPUT:
            return []
        if data_field.is_constant or not data_field.usages:
            return []
        ancestors = set(_ancestors(data_field))
        absolute = bool(ancestors & ABSOLUTE_COLLECTIONS)
        multitouch = bool(ancestors & MULTI_TOUCH_COLLECTIONS)
        pointer = bool(ancestors & POINTER_COLLECTIONS)
        if all(_is_key_usage(usage) for usage in data_field.usages):
            return self._bind_keys(data_field, pointer)
        return self._bind_axes(data_field, absolute, multitouch, pointer)

    def _bind_keys(self, data_field: Field, pointer: bool) -> list[Binding | None]:
        """A keyboard style field: one bit per key, or an array of usage indices."""
        usages = data_field.usages
        if data_field.size <= 1:
            # One bit per key, as used for modifiers and mouse buttons.
            return [
                self._make(
                    BindingKind.KEY,
                    data_field,
                    offset=data_field.offset + index,
                    size=1,
                    usage=usage,
                    index=index,
                    event_name=_key_name(usage, pointer),
                    pointer=pointer,
                )
                for index, usage in enumerate(usages[: data_field.count])
            ]
        # An array: each element holds either a usage index or a whole key.
        array = len(usages) > 1
        return [
            self._make(
                BindingKind.INDEX if array else BindingKind.KEY,
                data_field,
                offset=data_field.offset + index * data_field.size,
                size=data_field.size,
                usage=usages[index] if index < len(usages) else usages[0],
                index=index,
                event_name=None if array else _key_name(usages[0], pointer),
                pointer=pointer,
                array_base=_array_base(usages) if array else 1,
            )
            for index in range(data_field.count)
        ]

    def _bind_axes(
        self, data_field: Field, absolute: bool, multitouch: bool, pointer: bool
    ) -> list[Binding | None]:
        """A field whose elements are numbers: a stick, a wheel, a position."""
        bindings: list[Binding | None] = []
        for index in range(data_field.count):
            if index < len(data_field.usages):
                usage = data_field.usages[index]
            else:
                # A repeated axis with a single usage, e.g. an accelerometer.
                usage = data_field.usages[0]
            kind, name, axis = self._axis_for(usage, absolute, multitouch, pointer)
            if kind is None:
                self._unsupported(usage)
                continue
            element_size = 1 if data_field.size == 0 else data_field.size
            step = element_size
            bindings.append(
                self._make(
                    kind,
                    data_field,
                    offset=data_field.offset + index * step,
                    size=element_size,
                    usage=usage,
                    index=index,
                    event_name=name,
                    axis=axis,
                    finger=self._finger_of(data_field),
                    pointer=pointer,
                )
            )
        return bindings

    def _axis_for(
        self, usage: Usage, absolute: bool, multitouch: bool, pointer: bool
    ) -> tuple[BindingKind | None, str | None, int]:
        """Classify a non key usage as ``(kind, event name, axis index)``."""
        page, usage_id = usage.page, usage.id
        if page == GENERIC_DESKTOP_PAGE:
            if usage_id == 0x39:  # hat switch
                return BindingKind.HAT, None, 0
            if usage_id == 0x3A:  # joystick switch
                return BindingKind.KEY, "BTN_TRIGGER", 0
            if usage_id in DESKTOP_BUTTON_EVENTS:
                return BindingKind.KEY, DESKTOP_BUTTON_EVENTS[usage_id], 0
            if absolute and usage_id in ABSOLUTE_AXES:
                return BindingKind.ABS, ABSOLUTE_AXES[usage_id], 0
            if usage_id in RELATIVE_AXES:
                return BindingKind.REL, RELATIVE_AXES[usage_id], 0
            return None, None, 0
        if page == DIGITIZER_PAGE:
            if usage_id in DIGITIZER_TOUCH_USAGES:  # finger, touch, tip switch
                return BindingKind.TOUCH, None, 0
            # The contact identifier (0x51) and the contact count (0x54) are
            # bookkeeping: the slots come from the collections instead.
            name = DIGITIZER_EVENTS.get(usage_id)
            if name is None:
                return None, None, 0
            if multitouch and usage_id in _MT_AXIS_EVENTS:
                mt_name, axis = _MT_AXIS_EVENTS[usage_id]
                return BindingKind.MT_AXIS, mt_name, axis
            kind = BindingKind.ABS if name.startswith("ABS_") else BindingKind.KEY
            return kind, name, 0
        return None, None, 0

    def _make(
        self,
        kind: BindingKind,
        data_field: Field,
        *,
        offset: int,
        size: int,
        usage: Usage | None,
        index: int,
        event_name: str | None = None,
        axis: int = 0,
        finger: int = 0,
        pointer: bool = False,
        array_base: int = 1,
    ) -> Binding | None:
        event: Event | None = None
        if event_name is not None:
            event = event_code(event_name)
            if event is None or not self._sink.supports(*event):
                self._unsupported(usage, event_name)
                return None
        elif kind not in (
            BindingKind.KEY,
            BindingKind.HAT,
            BindingKind.TOUCH,
            BindingKind.INDEX,
        ):
            return None
        return Binding(
            kind=kind,
            report_id=data_field.report_id,
            offset=offset,
            size=size,
            signed=data_field.is_signed,
            usage=usage,
            field=data_field,
            event=event,
            event_name=event_name,
            index=index,
            axis=axis,
            finger=finger,
            pointer=pointer,
            array_base=array_base,
        )

    def _unsupported(self, usage: Usage | None, event_name: str | None = None) -> None:
        detail = describe_usage(usage.page, usage.id) if usage else "<no usage>"
        if event_name:
            detail += f" -> {event_name}"
        self.unsupported.add(detail)

    # -- runtime ----------------------------------------------------------

    def _reset(self) -> None:
        """Forget all transient state, so that nothing stays held down."""
        self._layout = {
            report_id: [dataclasses.replace(b, value=0) for b in bindings]
            for report_id, bindings in self._layout.items()
        }
        self._index_state.clear()
        self._hat_state.clear()
        self._down.clear()

    def handle_input_report(self, data: bytes) -> None:
        """Translate one HID input report (report ID byte included, if used)."""
        if self._descriptor.uses_report_ids:
            if not data:
                return
            report_id, payload = data[0], data[1:]
        else:
            report_id, payload = 0, data
        bindings = self._layout.get(report_id)
        if bindings is None:
            self._unknown_report(report_id)
            return
        for binding in bindings:
            try:
                value = read_bits(
                    payload, binding.offset, binding.size, signed=binding.signed
                )
            except IndexError:
                self._truncated_report(report_id, binding)
                return
            self._apply(binding, value)
        self._sink.sync()

    def _apply(self, binding: Binding, value: int) -> None:
        match binding.kind:
            case BindingKind.KEY:
                self._apply_key(binding, value)
            case BindingKind.INDEX:
                self._apply_index(binding, value)
            case BindingKind.REL:
                if value and binding.event is not None:
                    self._sink.emit(*binding.event, value)
            case BindingKind.ABS:
                self._apply_absolute(binding, value)
            case BindingKind.HAT:
                self._apply_hat(binding, value)
            case BindingKind.TOUCH:
                self._apply_touch(binding, value)
            case BindingKind.MT_AXIS:
                self._apply_mt_axis(binding, value)

    def _apply_key(self, binding: Binding, value: int) -> None:
        if binding.event is None:
            return
        pressed = 1 if value else 0
        if pressed != binding.value:
            self._sink.emit(*binding.event, pressed)
            binding.value = pressed

    def _apply_index(self, binding: Binding, value: int) -> None:
        """A keyboard array slot, whose value selects one of the field's usages."""
        state_key = (binding.report_id, binding.index)
        previous = self._index_state.get(state_key, 0)
        if value == previous:
            return
        if previous:
            self._release_usage(self._array_usage(binding, previous), binding.pointer)
        self._press_usage(self._array_usage(binding, value), binding.pointer)
        self._index_state[state_key] = value

    @staticmethod
    def _array_usage(binding: Binding, value: int) -> Usage | None:
        """The usage an array element currently points at, if any."""
        usages = binding.field.usages
        index = value - binding.array_base
        if 0 <= index < len(usages):
            return usages[index]
        return None

    def _press_usage(self, usage: Usage | None, pointer: bool) -> None:
        event = _key_event(self._sink, usage, pointer)
        if event is not None:
            self._sink.emit(*event, 1)

    def _release_usage(self, usage: Usage | None, pointer: bool) -> None:
        event = _key_event(self._sink, usage, pointer)
        if event is not None:
            self._sink.emit(*event, 0)

    def _apply_absolute(self, binding: Binding, value: int) -> None:
        if binding.event is None:
            return
        scaled = _scale(value, binding.field, self._sink, binding.event[1])
        if scaled == binding.value:
            return
        self._sink.emit(*binding.event, scaled)
        binding.value = scaled

    def _apply_mt_axis(self, binding: Binding, value: int) -> None:
        if binding.event is None:
            return
        scaled = _scale(value, binding.field, self._sink, binding.event[1])
        if self._mt.is_down(binding.finger):
            # A contact is on the surface: its position belongs to its own
            # slot, and is repeated whenever the slot changes.
            if self._mt.select(binding.finger) or scaled != binding.value:
                self._sink.emit(*binding.event, scaled)
        elif scaled != binding.value:
            # No contact: keep the single touch axis in step with the finger.
            self._sink.emit(*binding.event, scaled)
        binding.value = scaled

    def _apply_hat(self, binding: Binding, value: int) -> None:
        state_key = (binding.report_id, binding.index)
        previous = self._hat_state.get(state_key, ())
        current = self._hat_table.get(value, ())
        if current == previous:
            return
        for name in previous:
            if name not in current:
                self._emit_named(name, 0)
        for name in current:
            if name not in previous:
                self._emit_named(name, 1)
        self._hat_state[state_key] = current

    def _emit_named(self, name: str, value: int) -> None:
        event = event_code(name)
        if event is not None and self._sink.supports(*event):
            self._sink.emit(*event, value)

    def _apply_touch(self, binding: Binding, value: int) -> None:
        finger, down = binding.finger, bool(value)
        if down == (finger in self._down):
            return
        if down:
            self._down.add(finger)
            self._mt.down(finger)
        else:
            self._down.discard(finger)
            self._mt.up(finger)

    def _unknown_report(self, report_id: int) -> None:
        name = f"input report id {report_id} is not in the report descriptor"
        if name not in self.unsupported:
            self.unsupported.add(name)
            logger.warning("ignoring %s", name)

    def _truncated_report(self, report_id: int, binding: Binding) -> None:
        name = f"input report id {report_id} is too short ({binding} was truncated)"
        if name not in self.unsupported:
            self.unsupported.add(name)
            logger.warning("ignoring %s", name)

    def release_all(self) -> None:
        """Release everything this converter is holding down.

        Called when a session ends: without it, a key that was pressed when the
        link dropped would stay pressed for as long as the device exists.
        """
        for bindings in self._layout.values():
            for binding in bindings:
                self._release(binding)
        self._reset()
        self._mt.release_all()
        self._sink.sync()

    def _release(self, binding: Binding) -> None:
        match binding.kind:
            case BindingKind.KEY:
                if binding.value and binding.event is not None:
                    self._sink.emit(*binding.event, 0)
            case BindingKind.INDEX:
                held = self._index_state.get((binding.report_id, binding.index), 0)
                if held:
                    self._release_usage(self._array_usage(binding, held), binding.pointer)
            case BindingKind.HAT:
                for name in self._hat_state.get((binding.report_id, binding.index), ()):
                    self._emit_named(name, 0)
            case _:
                pass  # axes and contacts have no held down state of their own


def _key_event(
    sink: EventSink, usage: Usage | None, pointer: bool = False
) -> Event | None:
    name = _key_name(usage, pointer)
    if name is None:
        return None
    event = event_code(name)
    if event is None or not sink.supports(*event):
        return None
    return event


def _key_name(usage: Usage | None, pointer: bool = False) -> str | None:
    """The evdev name of a key or button usage, or ``None`` if there is none."""
    if usage is None:
        return None
    if usage.page == KEYBOARD_PAGE:
        return KEYBOARD_PAGE_EVENTS.get(usage.id)
    if usage.page == BUTTON_PAGE:
        buttons = POINTER_BUTTON_EVENTS if pointer else GAMEPAD_BUTTON_EVENTS
        return buttons.get(usage.id)
    if usage.page == CONSUMER_PAGE:
        return consumer_event(usage.id)
    if usage.page == GENERIC_DESKTOP_PAGE:
        return generic_desktop_event(usage.id)
    return None


def _scale(value: int, data_field: Field, sink: EventSink, ev_code: int) -> int:
    """Map a raw value onto the range the uinput device declared for ``ev_code``."""
    limits = sink.abs_range(ev_code)
    if limits is None:
        return int(value)
    low, high = data_field.logical_min, data_field.logical_max
    abs_min, abs_max = limits
    if high <= low:
        return max(abs_min, min(abs_max, int(value)))
    scaled = abs_min + round((value - low) * (abs_max - abs_min) / (high - low))
    return max(abs_min, min(abs_max, scaled))
