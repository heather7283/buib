"""Report descriptors taken from real devices, as byte strings.

Keeping the raw bytes in the tests (rather than building them programmatically)
keeps the tests honest: this is exactly what arrives over SDP.
"""

from __future__ import annotations

#: The boot keyboard descriptor from the HID specification, appendix E.6.
BOOT_KEYBOARD = bytes.fromhex(
    "05 01 09 06 a1 01"  # Usage Page (Generic Desktop), Usage (Keyboard)
    "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"  # 8 modifier bits
    "95 01 75 08 81 03"  # reserved byte
    "95 05 75 01 05 08 19 01 29 05 91 02"  # 5 LED bits (output)
    "95 01 75 03 91 03"  # 3 padding bits
    "95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00"  # 6 keycodes, 0..101
    "c0"
)

#: A very common mouse descriptor: three buttons, X, Y and a wheel.
MOUSE = bytes.fromhex(
    "05 01 09 02 a1 01"  # Usage Page (Generic Desktop), Usage (Mouse)
    "09 01 a1 00"  # Usage (Pointer)
    "05 09 19 01 29 03 15 00 25 01 95 03 75 01 81 02"  # button 1..3
    "95 01 75 05 81 01"  # 5 padding bits
    "05 01 09 30 09 31 15 81 25 7f 75 08 95 01 81 06"  # X, Y: -127..127
    "05 01 09 38 15 81 25 7f 75 08 95 01 81 06"  # wheel: -127..127
    "c0"
    "c0"
)

#: A gamepad with an 8 way hat switch and four axes, using report ID 1.
GAMEPAD = bytes.fromhex(
    "05 01 09 05 a1 01"  # Usage Page (Generic Desktop), Usage (Game Pad)
    "85 01"  #   Report ID (1)
    "05 09 19 01 29 0a 15 00 25 01 75 01 95 0a 81 02"  #   buttons 1..10
    "75 01 95 02 81 03"  #   2 padding bits
    "05 01 09 39 15 00 25 07 75 04 95 01 81 02"  #   hat switch, 0..7
    "75 04 95 01 81 03"  #   4 padding bits
    "05 01 09 01 a1 00"  #   Usage (Pointer)
    "09 30 09 31 15 00 26 ff 00 75 10 95 01 81 02"  #     X, Y: 0..255, 16 bits
    "09 32 09 35 15 00 26 ff 00 75 08 95 02 81 02"  #     Z, Rz: 0..255, 8 bits
    "c0"
    "c0"
)

#: A two finger touchpad: a contact identifier, a touch flag and X/Y per finger.
TOUCHPAD = bytes.fromhex(
    "05 0d 09 05 a1 01"  # Usage Page (Digitizer), Usage (Touchpad)
    "05 0d 09 03 a1 01"  #   Usage (Finger)
    "05 0d 19 01 29 05 15 00 25 01 75 01 95 05 81 02"  #     contact count
    "75 01 95 01 15 00 25 01 81 02"  #     padding
    "05 0d 09 22 15 00 25 01 75 01 95 01 81 02"  #     finger present
    "05 0d 09 30 09 31 15 00 25 7f 75 08 95 02 81 02"  #     X, Y: 0..127
    "c0"
    "c0"
)

#: A keyboard with two report IDs: one for regular keys, one for the F keys.
MULTI_REPORT_KEYBOARD = bytes.fromhex(
    "05 01 09 06 a1 01"
    "85 01"  # Report ID (1)
    "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"
    "95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00"
    "85 02"  # Report ID (2)
    "05 07 19 3a 29 3f 15 00 25 01 75 01 95 06 81 02"  # six one bit keys
    "c0"
)

#: A keyboard that also has a volume up/down pair on the consumer page.
MULTIMEDIA_KEYBOARD = bytes.fromhex(
    "05 01 09 06 a1 01"
    "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"
    "95 01 75 08 81 03"
    "95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00"
    "05 0c 09 0a 09 23 09 b0 15 00 25 01 75 01 95 03 81 02"  # mute, cut, copy
    "c0"
)
