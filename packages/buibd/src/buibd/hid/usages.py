"""HID usage tables and the mapping from HID usages to evdev events.

The tables map a ``(usage page, usage)`` pair to the *name* of an ``uinput``
constant (``"KEY_A"``, ``"REL_X"``, ...).  Names rather than raw numbers keep
the tables readable and let :func:`buibd.evdev.event_code` fail loudly when a
name does not exist in the ``uinput`` module.

Not every usage can be mapped: anything missing here is reported to the user as
an unsupported usage instead of being silently dropped.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ABSOLUTE_AXES",
    "BUTTON_PAGE",
    "CONSUMER_PAGE",
    "DIGITIZER_EVENTS",
    "DIGITIZER_FINGER_USAGES",
    "DIGITIZER_PAGE",
    "DIGITIZER_TOUCH_USAGES",
    "GAMEPAD_BUTTON_EVENTS",
    "GENERIC_DESKTOP_PAGE",
    "KEYBOARD_PAGE",
    "KEYBOARD_PAGE_EVENTS",
    "POINTER_BUTTON_EVENTS",
    "RELATIVE_AXES",
    "describe_usage",
]

# ---------------------------------------------------------------------------
# Usage pages
GENERIC_DESKTOP_PAGE: Final = 0x01
SIMULATION_PAGE: Final = 0x02
KEYBOARD_PAGE: Final = 0x07
LED_PAGE: Final = 0x08
BUTTON_PAGE: Final = 0x09
CONSUMER_PAGE: Final = 0x0C
DIGITIZER_PAGE: Final = 0x0D

# ---------------------------------------------------------------------------
# Human readable names, used for diagnostics only.

_PAGE_NAMES: Final = {
    GENERIC_DESKTOP_PAGE: "Generic Desktop",
    SIMULATION_PAGE: "Simulation",
    KEYBOARD_PAGE: "Keyboard/Keypad",
    LED_PAGE: "LED",
    BUTTON_PAGE: "Button",
    CONSUMER_PAGE: "Consumer",
    DIGITIZER_PAGE: "Digitizer",
}

#: Generic Desktop usages that name a device class (a top level collection).
DESKTOP_DEVICE_NAMES: Final = {
    0x01: "Pointer",
    0x02: "Mouse",
    0x05: "Game Pad",
    0x06: "Keyboard",
    0x08: "Multi-Axis Controller",
    0x09: "Tablet",
    0x0D: "Touchpad",
}

#: Digitizer usages that name a device class (a top level collection).
DIGITIZER_DEVICE_NAMES: Final = {
    0x02: "Pen",
    0x03: "Finger",
    0x05: "Touchpad",
}

_DIGITIZER_USAGE_NAMES: Final = {
    0x02: "Pen",
    0x03: "Finger",
    0x04: "Queue",
    0x05: "Touchpad",
    0x22: "Finger",
    0x23: "Pen",
    0x30: "X",
    0x31: "Y",
    0x32: "Z",
    0x39: "Button Touch",
    0x3A: "In Range",
    0x42: "Tip Switch",
    0x44: "Barrel Switch",
    0x45: "Eraser",
    0x4A: "Pressure",
    0x4B: "Width",
    0x4C: "Height",
    0x51: "Contact Identifier",
    0x54: "Contact Count",
}


def describe_usage(page: int, usage_id: int) -> str:
    """Render a usage as ``0x09:0x05 (Button 5)`` for log and error messages."""
    name = _DIGITIZER_USAGE_NAMES.get(usage_id) if page == DIGITIZER_PAGE else None
    if name is None:
        name = (
            DESKTOP_DEVICE_NAMES.get(usage_id) if page == GENERIC_DESKTOP_PAGE else None
        )
    if name is None:
        name = _PAGE_NAMES.get(page, f"Page 0x{page:02X}")
    return f"0x{page:02X}:0x{usage_id:02X} ({name} {usage_id:#04x})"


# ---------------------------------------------------------------------------
# Keyboard/Keypad page (0x07).  Note that the HID usages are in alphabetical
# order, so the mapping to evdev is a table and not an offset: usage 0x04 is
# "a" and 0x1D is "z".

KEYBOARD_PAGE_EVENTS: Final[dict[int, str]] = {
    # USB HID usage table 12: the letters are in alphabetical order,
    # the digits and the rest follow them.
    0x04: "KEY_A",
    0x05: "KEY_B",
    0x06: "KEY_C",
    0x07: "KEY_D",
    0x08: "KEY_E",
    0x09: "KEY_F",
    0x0A: "KEY_G",
    0x0B: "KEY_H",
    0x0C: "KEY_I",
    0x0D: "KEY_J",
    0x0E: "KEY_K",
    0x0F: "KEY_L",
    0x10: "KEY_M",
    0x11: "KEY_N",
    0x12: "KEY_O",
    0x13: "KEY_P",
    0x14: "KEY_Q",
    0x15: "KEY_R",
    0x16: "KEY_S",
    0x17: "KEY_T",
    0x18: "KEY_U",
    0x19: "KEY_V",
    0x1A: "KEY_W",
    0x1B: "KEY_X",
    0x1C: "KEY_Y",
    0x1D: "KEY_Z",
    0x1E: "KEY_1",
    0x1F: "KEY_2",
    0x20: "KEY_3",
    0x21: "KEY_4",
    0x22: "KEY_5",
    0x23: "KEY_6",
    0x24: "KEY_7",
    0x25: "KEY_8",
    0x26: "KEY_9",
    0x27: "KEY_0",
    0x28: "KEY_ENTER",
    0x29: "KEY_ESC",
    0x2A: "KEY_BACKSPACE",
    0x2B: "KEY_TAB",
    0x2C: "KEY_SPACE",
    0x2D: "KEY_MINUS",
    0x2E: "KEY_EQUAL",
    0x2F: "KEY_LEFTBRACE",
    0x30: "KEY_RIGHTBRACE",
    0x31: "KEY_BACKSLASH",
    0x32: "KEY_102ND",
    0x33: "KEY_SEMICOLON",
    0x34: "KEY_APOSTROPHE",
    0x35: "KEY_GRAVE",
    0x36: "KEY_COMMA",
    0x37: "KEY_DOT",
    0x38: "KEY_SLASH",
    0x39: "KEY_CAPSLOCK",
    0x3A: "KEY_F1",
    0x3B: "KEY_F2",
    0x3C: "KEY_F3",
    0x3D: "KEY_F4",
    0x3E: "KEY_F5",
    0x3F: "KEY_F6",
    0x40: "KEY_F7",
    0x41: "KEY_F8",
    0x42: "KEY_F9",
    0x43: "KEY_F10",
    0x44: "KEY_F11",
    0x45: "KEY_F12",
    0x46: "KEY_SYSRQ",
    0x47: "KEY_SCROLLLOCK",
    0x48: "KEY_PAUSE",
    0x49: "KEY_INSERT",
    0x4A: "KEY_HOME",
    0x4B: "KEY_PAGEUP",
    0x4C: "KEY_DELETE",
    0x4D: "KEY_END",
    0x4E: "KEY_PAGEDOWN",
    0x4F: "KEY_RIGHT",
    0x50: "KEY_LEFT",
    0x51: "KEY_DOWN",
    0x52: "KEY_UP",
    0x53: "KEY_NUMLOCK",
    0x54: "KEY_KPSLASH",
    0x55: "KEY_KPASTERISK",
    0x56: "KEY_KPMINUS",
    0x57: "KEY_KPPLUS",
    0x58: "KEY_KPENTER",
    0x59: "KEY_KP1",
    0x5A: "KEY_KP2",
    0x5B: "KEY_KP3",
    0x5C: "KEY_KP4",
    0x5D: "KEY_KP5",
    0x5E: "KEY_KP6",
    0x5F: "KEY_KP7",
    0x60: "KEY_KP8",
    0x61: "KEY_KP9",
    0x62: "KEY_KP0",
    0x63: "KEY_KPDOT",
    0x64: "KEY_102ND",
    0x65: "KEY_MENU",
    0x66: "KEY_POWER",
    0x67: "KEY_KPEQUAL",
    0x68: "KEY_F13",
    0x69: "KEY_F14",
    0x6A: "KEY_F15",
    0x6B: "KEY_F16",
    0x6C: "KEY_F17",
    0x6D: "KEY_F18",
    0x6E: "KEY_F19",
    0x6F: "KEY_F20",
    0x70: "KEY_F21",
    0x71: "KEY_F22",
    0x72: "KEY_F23",
    0x73: "KEY_F24",
    0x75: "KEY_HELP",
    0x76: "KEY_MENU",
    0x77: "KEY_SELECT",
    0x78: "KEY_STOP",
    0x79: "KEY_AGAIN",
    0x7A: "KEY_UNDO",
    0x7B: "KEY_CUT",
    0x7C: "KEY_COPY",
    0x7D: "KEY_PASTE",
    0x7E: "KEY_FIND",
    0x7F: "KEY_MUTE",
    0x80: "KEY_VOLUMEUP",
    0x81: "KEY_VOLUMEDOWN",
    0x85: "KEY_KPCOMMA",
    0x88: "KEY_KATAKANA",
    0x89: "KEY_HIRAGANA",
    0x8A: "KEY_HENKAN",
    0x8B: "KEY_KATAKANAHIRAGANA",
    0x8C: "KEY_MUHENKAN",
    0x8D: "KEY_KPJPCOMMA",
    0x90: "KEY_KATAKANA",
    0x91: "KEY_KATAKANA",
    0x92: "KEY_HIRAGANA",
    0xE0: "KEY_LEFTCTRL",
    0xE1: "KEY_LEFTSHIFT",
    0xE2: "KEY_LEFTALT",
    0xE3: "KEY_LEFTMETA",
    0xE4: "KEY_RIGHTCTRL",
    0xE5: "KEY_RIGHTSHIFT",
    0xE6: "KEY_RIGHTALT",
    0xE7: "KEY_RIGHTMETA",
}
#: The eight modifier bits, which is how keyboards report Ctrl/Alt/Shift/GUI.
KEYBOARD_MODIFIERS: Final[tuple[int, ...]] = (
    0xE0,
    0xE1,
    0xE2,
    0xE3,
    0xE4,
    0xE5,
    0xE6,
    0xE7,
)

# ---------------------------------------------------------------------------
# Button page (0x09).  The same usage means "left click" in a mouse and
# "south" (the bottom face button) in a gamepad, so the two readings are kept
# apart and the converter picks one from the enclosing collection.

#: Gamepads in "report" protocol mode use usages 1..12 for the face buttons,
#: the shoulders, start/select/mode and the thumbstick clicks.
GAMEPAD_BUTTON_EVENTS: Final[dict[int, str]] = {
    0x01: "BTN_SOUTH",
    0x02: "BTN_EAST",
    0x03: "BTN_NORTH",
    0x04: "BTN_WEST",
    0x05: "BTN_TL",
    0x06: "BTN_TR",
    0x07: "BTN_TL2",
    0x08: "BTN_TR2",
    0x09: "BTN_SELECT",
    0x0A: "BTN_START",
    0x0B: "BTN_MODE",
    0x0C: "BTN_THUMBL",
}

#: Pointers use the first three usages for the primary, secondary and middle
#: buttons; the rest turn up on mice with extra buttons.
POINTER_BUTTON_EVENTS: Final[dict[int, str]] = {
    0x01: "BTN_LEFT",
    0x02: "BTN_RIGHT",
    0x03: "BTN_MIDDLE",
    0x04: "BTN_SIDE",
    0x05: "BTN_EXTRA",
    0x06: "BTN_BACK",
    0x07: "BTN_FORWARD",
}

#: Generic Desktop aliases of Button 1..4, used by some gamepad descriptors.
DESKTOP_BUTTON_EVENTS: Final[dict[int, str]] = {
    0x03: "BTN_SOUTH",
    0x04: "BTN_EAST",
    0x05: "BTN_NORTH",
    0x06: "BTN_WEST",
}

# ---------------------------------------------------------------------------
# Generic Desktop.  X/Y/Z and friends are relative for a mouse and absolute for
# a gamepad, so they live in the two tables below and are chosen by the
# converter based on the enclosing collection.

RELATIVE_AXES: Final[dict[int, str]] = {
    0x30: "REL_X",
    0x31: "REL_Y",
    0x32: "REL_Z",
    0x33: "REL_RX",
    0x34: "REL_RY",
    0x35: "REL_RZ",
    0x38: "REL_WHEEL",
}

ABSOLUTE_AXES: Final[dict[int, str]] = {
    0x30: "ABS_X",
    0x31: "ABS_Y",
    0x32: "ABS_Z",
    0x33: "ABS_RX",
    0x34: "ABS_RY",
    0x35: "ABS_RZ",
    0x36: "ABS_RUDDER",
    0x37: "ABS_THROTTLE",
    0x38: "ABS_WHEEL",
    0x39: "ABS_GAS",
    0x3A: "ABS_BRAKE",
    0x40: "ABS_Z",
}

#: Generic Desktop system controls (power, sleep, ...).
DESKTOP_CONTROL_EVENTS: Final[dict[int, str]] = {
    0x81: "KEY_POWER",
    0x82: "KEY_SLEEP",
    0x83: "KEY_WAKEUP",
    0x84: "KEY_CONTEXT_MENU",
    0x85: "KEY_COMPUTER",
    0x86: "KEY_FORWARD",
    0x87: "KEY_BACK",
    0x88: "KEY_HOMEPAGE",
    0x89: "KEY_REFRESH",
    0x8A: "KEY_STOP",
    0x9B: "KEY_ADDRESSBOOK",
    0x9C: "KEY_CALENDAR",
    0xA0: "KEY_COFFEE",
    0xA3: "KEY_ROTATE_DISPLAY",
    0xA4: "KEY_CYCLEWINDOWS",
    0xA5: "KEY_ZOOMIN",
    0xA6: "KEY_ZOOMOUT",
    0xA7: "KEY_SCREENSAVER",
    0xA8: "KEY_DISPLAY_OFF",
    0xB2: "KEY_PROG1",
    0xB3: "KEY_PROG2",
    0xB4: "KEY_ADDRESSBOOK",
    0xB5: "KEY_CALENDAR",
    0xB6: "KEY_ADDRESSBOOK",
    0xB7: "KEY_PHONE",
    0xB8: "KEY_WIMAX",
}

#: Vendor defined 0xFF00..0xFFFF block, where several vendors hide media keys.
_VENDOR_MEDIA: Final[dict[int, str]] = {
    0x100: "KEY_PLAYPAUSE",
    0x110: "KEY_PREVIOUSSONG",
    0x111: "KEY_NEXTSONG",
    0x112: "KEY_STOPCD",
    0x113: "KEY_VOLUMEUP",
    0x114: "KEY_VOLUMEDOWN",
    0x115: "KEY_MUTE",
    0x116: "KEY_MENU",
}

# ---------------------------------------------------------------------------
# Consumer page.  Media remotes and the extra keys of multimedia keyboards.

CONSUMER_PAGE_EVENTS: Final[dict[int, str]] = {
    0x0A: "KEY_MUTE",
    0x0B: "KEY_VOLUMEUP",
    0x0C: "KEY_VOLUMEDOWN",
    0x0D: "KEY_CHANNELUP",
    0x0E: "KEY_CHANNELDOWN",
    0x19: "KEY_NEXTSONG",
    0x1A: "KEY_PREVIOUSSONG",
    0x1B: "KEY_STOPCD",
    0x1C: "KEY_PLAYPAUSE",
    0x20: "KEY_MAIL",
    0x21: "KEY_CALC",
    0x23: "KEY_CUT",
    0x24: "KEY_FILE",
    0x25: "KEY_WWW",
    0x26: "KEY_MSDOS",
    0x29: "KEY_SCREENLOCK",
    0x2A: "KEY_POWER",
    0x2B: "KEY_SLEEP",
    0x2C: "KEY_WAKEUP",
    0x30: "KEY_PLAYPAUSE",
    0x31: "KEY_PAUSE",
    0x32: "KEY_RECORD",
    0x33: "KEY_FASTFORWARD",
    0x34: "KEY_REWIND",
    0x35: "KEY_NEXTSONG",
    0x36: "KEY_PREVIOUSSONG",
    0x37: "KEY_STOPCD",
    0x38: "KEY_EJECTCD",
    0x40: "KEY_MEDIA",
    0x41: "KEY_UP",
    0x42: "KEY_DOWN",
    0x43: "KEY_LEFT",
    0x44: "KEY_RIGHT",
    0x45: "KEY_ENTER",
    0x50: "KEY_CAMERA",
    0x65: "KEY_CALC",
    0x6C: "KEY_MENU",
    0x70: "KEY_INFO",
    0x73: "KEY_SEARCH",
    0x77: "KEY_STOP",
    0x7C: "KEY_SCREENSAVER",
    0x7E: "KEY_SLEEP",
    # Application launch buttons.
    0xB0: "KEY_PLAYPAUSE",
    0xB1: "KEY_PAUSE",
    0xB2: "KEY_RECORD",
    0xB3: "KEY_FASTFORWARD",
    0xB4: "KEY_REWIND",
    0xB5: "KEY_NEXTSONG",
    0xB6: "KEY_PREVIOUSSONG",
    0xB7: "KEY_STOPCD",
    0xB8: "KEY_EJECTCD",
    0xBC: "KEY_MUTE",
    0xBD: "KEY_BASSBOOST",
    0xE2: "KEY_VOLUMEUP",
    0xE3: "KEY_VOLUMEDOWN",
    0xE4: "KEY_MUTE",
    0xE9: "KEY_VOLUMEUP",
    0xEA: "KEY_VOLUMEDOWN",
    0xEB: "KEY_MUTE",
    0x100: "KEY_MEDIA",
    0x101: "KEY_GAMES",
    0x102: "KEY_ZOOMIN",
    0x103: "KEY_ZOOMOUT",
    0x104: "KEY_ZOOMRESET",
    0x105: "KEY_WORDPROCESSOR",
    0x106: "KEY_EDITOR",
    0x107: "KEY_SPREADSHEET",
    0x108: "KEY_PRESENTATION",
    0x109: "KEY_DATABASE",
    0x10A: "KEY_NEWS",
    0x10B: "KEY_VOICEMAIL",
    0x10C: "KEY_ADDRESSBOOK",
    0x10D: "KEY_MESSENGER",
    0x10E: "KEY_DISPLAYTOGGLE",
    0x110: "KEY_SPELLCHECK",
    0x111: "KEY_LOGOFF",
    0x112: "KEY_DOLLAR",
    0x115: "KEY_EURO",
    0x116: "KEY_FRAMEBACK",
    0x117: "KEY_FRAMEFORWARD",
    0x118: "KEY_CONTEXT_MENU",
    0x119: "KEY_MEDIA_REPEAT",
    0x11A: "KEY_10CHANNELSUP",
    0x11B: "KEY_10CHANNELSDOWN",
    0x11E: "KEY_MICMUTE",
    0x120: "KEY_INFO",
    0x122: "KEY_SUSPEND",
    0x124: "KEY_CLOSE",
    0x12A: "KEY_BRIGHTNESSUP",
    0x12B: "KEY_BRIGHTNESSDOWN",
    0x12C: "KEY_KBDILLUMTOGGLE",
    0x12D: "KEY_KBDILLUMDOWN",
    0x12E: "KEY_KBDILLUMUP",
    0x12F: "KEY_SEND",
    0x130: "KEY_REPLY",
    0x131: "KEY_FORWARDMAIL",
    0x132: "KEY_SAVE",
    0x133: "KEY_DOCUMENTS",
    0x134: "KEY_BATTERY",
    0x135: "KEY_BLUETOOTH",
    0x136: "KEY_WLAN",
    0x137: "KEY_UWB",
    0x138: "KEY_UNKNOWN",
    0x139: "KEY_VIDEO_NEXT",
    0x13A: "KEY_VIDEO_PREV",
    0x13C: "KEY_BRIGHTNESS_CYCLE",
    0x13D: "KEY_BRIGHTNESS_AUTO",
    0x13E: "KEY_DISPLAY_OFF",
    0x140: "KEY_WIMAX",
    0x141: "KEY_RFKILL",
    0x142: "KEY_MICMUTE",
    0x146: "KEY_CYCLEWINDOWS",
    0x147: "KEY_DIRECTION",
    0x148: "KEY_ROTATE_DISPLAY",
    0x149: "KEY_DASHBOARD",
    0x14A: "KEY_SUSPEND",
    0x14B: "KEY_TASKMANAGER",
    0x14C: "KEY_SCREENSAVER",
    0x14D: "KEY_STOP",
    0x14E: "KEY_ADDRESSBOOK",
    0x14F: "KEY_SELECT",
    0x150: "KEY_STOPCD",
    0x15D: "KEY_CONFIG",
    0x161: "KEY_DIRECTORY",
    0x162: "KEY_LIST",
    0x163: "KEY_MEMO",
    0x164: "KEY_CALENDAR",
    0x165: "KEY_CALENDAR",
    0x166: "KEY_NEW",
    0x167: "KEY_WWW",
    0x168: "KEY_CD",
    0x169: "KEY_CALC",
    0x16A: "KEY_MEMO",
    0x16B: "KEY_DASHBOARD",
    0x16C: "KEY_ADDRESSBOOK",
    0x16D: "KEY_LOGOFF",
    0x16E: "KEY_HOMEPAGE",
    0x16F: "KEY_DOCUMENTS",
    0x170: "KEY_BATTERY",
    0x171: "KEY_BLUETOOTH",
    0x172: "KEY_WLAN",
    0x173: "KEY_UWB",
    0x180: "KEY_CAMERA",
    0x181: "KEY_CAMERA_FOCUS",
    0x182: "KEY_CAMERA_LEFT",
    0x183: "KEY_CAMERA_RIGHT",
    0x185: "KEY_CAMERA_ZOOMIN",
    0x186: "KEY_CAMERA_ZOOMOUT",
    0x187: "KEY_CAMERA_UP",
    0x188: "KEY_CAMERA_DOWN",
    0x18A: "KEY_ATTENDANT_ON",
    0x18B: "KEY_ATTENDANT_OFF",
    0x18C: "KEY_ATTENDANT_TOGGLE",
    0x190: "KEY_VOICEMAIL",
    0x191: "KEY_SEND",
    0x199: "KEY_TEEN",
    0x19A: "KEY_TWEN",
    0x1A0: "KEY_DIRECTORY",
}

# ---------------------------------------------------------------------------
# Digitizer page (touchpads and touch screens).

#: Data usages of the digitizer page.  The collection types (0x01 to 0x0F, such
#: as Finger or Touchpad) are deliberately absent: they never carry data.
DIGITIZER_EVENTS: Final[dict[int, str]] = {
    0x22: "BTN_TOUCH",
    0x23: "BTN_STYLUS",
    0x30: "ABS_X",
    0x31: "ABS_Y",
    0x32: "ABS_Z",
    0x39: "BTN_TOUCH",
    0x3A: "BTN_STYLUS",
    0x42: "BTN_TOUCH",
    0x44: "BTN_STYLUS2",
    0x45: "BTN_TOOL_RUBBER",
    0x4A: "ABS_PRESSURE",
    0x4B: "ABS_MT_WIDTH_MAJOR",
    0x4C: "ABS_MT_TOUCH_MAJOR",
}

#: Digitizer usages that say whether a contact is touching the surface.  The
#: contact identifier (0x51) is not one of them: it names a contact rather than
#: starting or ending one.
DIGITIZER_TOUCH_USAGES: Final[frozenset[int]] = frozenset({0x22, 0x39, 0x42})

#: Digitizer collection types that describe a single contact.
DIGITIZER_FINGER_USAGES: Final[frozenset[int]] = frozenset({0x02, 0x03, 0x04})

#: Collections that describe a pointing device rather than a gamepad.
POINTER_COLLECTIONS: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        (GENERIC_DESKTOP_PAGE, 0x01),  # Pointer
        (GENERIC_DESKTOP_PAGE, 0x02),  # Mouse
        (GENERIC_DESKTOP_PAGE, 0x04),  # Joystick
        (DIGITIZER_PAGE, 0x05),  # Touchpad
    }
)

#: Collections that mean "this is a gamepad", so X/Y/Z are absolute.
ABSOLUTE_COLLECTIONS: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        (GENERIC_DESKTOP_PAGE, 0x05),  # Game Pad
        (GENERIC_DESKTOP_PAGE, 0x08),  # Multi-Axis Controller
        (GENERIC_DESKTOP_PAGE, 0x09),  # Tablet
        (SIMULATION_PAGE, 0x01),  # Simulation Controls
        (SIMULATION_PAGE, 0x02),  # Actuators
    }
)

#: Collections that carry multi touch contacts.
MULTI_TOUCH_COLLECTIONS: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        (DIGITIZER_PAGE, 0x05),  # Touchpad
        (DIGITIZER_PAGE, 0x03),  # Finger
        (DIGITIZER_PAGE, 0x04),  # Queue
        (DIGITIZER_PAGE, 0x0A),  # Touch Screen
    }
)

#: Vendor pages start at 0xFF00; several vendors put media keys there.
VENDOR_PAGE_START: Final = 0xFF00


def consumer_event(usage_id: int) -> str | None:
    """Map a consumer page usage to an evdev event name.

    Handles the two encodings in the wild: the plain ``0x00`` block and the
    extended ``0x0B0``-style application block (values above ``0xFF``).
    """
    if usage_id in CONSUMER_PAGE_EVENTS:
        return CONSUMER_PAGE_EVENTS[usage_id]
    if usage_id >= VENDOR_PAGE_START:
        return _VENDOR_MEDIA.get(usage_id & 0xFF)
    return None


def generic_desktop_event(usage_id: int) -> str | None:
    """Map a generic desktop usage (buttons, system controls) to an event name."""
    return DESKTOP_BUTTON_EVENTS.get(usage_id) or DESKTOP_CONTROL_EVENTS.get(usage_id)
