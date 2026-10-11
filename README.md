# buib

**B**luetooth **u**input **b**ridge: connect to a Bluetooth HID device and
forward its reports to Linux as an ordinary input device, so that keyboards,
mice, touchpads and gamepads work over a bare Bluetooth adapter.

```
  Bluetooth HID device ──(L2CAP/HIDP)──► buibd ──(uinput)──► /dev/input/eventN
                                              ▲
                                         UNIX socket (NDJSON)
                                              │
                                           buibctl
```

## The two halves

| Package                                    | What it is                                             | Dependencies          |
| ------------------------------------------ | ------------------------------------------------------ | --------------------- |
| [`buib-protocol`](packages/buib-protocol)  | the NDJSON wire format, shared by both sides            | standard library only |
| [`buibd`](packages/buibd)                  | the daemon: owns the uinput device and the Bluetooth link | bumble, python-uinput |
| [`buibctl`](packages/buibctl)              | the client: `connect`, `disconnect`, `status`, …          | buib-protocol only    |

The client is a separate distribution on purpose: installing it pulls in no
Bluetooth stack, no adapter access and no `/dev/uinput` permission, so it can
be installed on any machine that can reach the daemon's socket.

```
$ uv tree --package buibctl
buibctl v0.1.0
└── buib-protocol v0.1.0
```

## Design rules

**The uinput device is the most important resource, and it is untouchable.**
It is created before anything Bluetooth happens, and the daemon holds it open
until it exits. No Bluetooth code path can close it, recreate it or resize it:

- a failed connection, a dropped ACL or an unparsable report descriptor are
  *status*, not fatal errors;
- when a session ends, the keys, buttons and contacts it was holding down are
  released — so nothing gets stuck — and the device stays registered;
- only `buibd` exiting destroys the device.

**Bluetooth is opened lazily.** A daemon whose adapter is missing, or unplugged
later, keeps running and keeps serving its socket; `buibctl status` says what
went wrong. The next `connect` tries again.

**Capabilities are fixed at startup.** The kernel copies a uinput device's
capabilities into the input device when it is created and offers no way to add
more, so the set has to be chosen when the daemon starts — see `--profile`.
This is the one thing that needs a restart to change.

## Install

Requires Python 3.11+, a Bluetooth adapter, and the `uinput` kernel module
(`modprobe uinput`).

```console
$ git clone <this repository> && cd buib
$ uv sync --all-packages          # a development environment
$ uv run buibd --help
```

To install the two commands system wide:

```console
$ uv tool install --from ./packages/buibctl buibctl   # the client, anywhere
$ uv pip install ./packages/buibd                    # the daemon, needs the group
```

`buibd` needs write access to `/dev/uinput`: run it as root, or add the user to
the `input` group (`usermod -aG input "$USER"`).

## Use

```console
# start the daemon (systemd users: see packaging/buibd.service)
$ buibd --profile universal

# in another terminal
$ buibctl status
state:    connected
device:   E1:CA:72:48:C4:E8
name:     MX Keys
for:      91.2s (482 reports)
uinput:
  device:       buib Bluetooth HID bridge [universal]
  capabilities: 412
  held down:    KEY_A
bluetooth: open (usb:0)
device details:
  battery level: 78
  report descriptor bytes: 149

$ buibctl connect E1:CA:72:48:C4:E8
$ buibctl disconnect
$ buibctl paired
$ buibctl unpair E1:CA:72:48:C4:E8
$ buibctl --json status        # the raw response payload
```

`buibctl` exit codes: `0` success, `1` the daemon refused, `2` bad usage,
`3` the daemon could not be reached.

### Pairing

The device has to be paired with _this_ host first. Either pair it with the
desktop's own Bluetooth stack, or start the daemon with `--pair`, which accepts
a pairing request when a device turns up that is not bonded yet. With BlueZ
running, pair the device there instead and start the daemon with
`--transport hci-socket` so both share the adapter.

### Options worth knowing

| Option              | Why                                                                                                   |
| ------------------- | ----------------------------------------------------------------------------------------------------- |
| `--profile NAME`    | `universal` (default), `keyboard`, `mouse`, `gamepad`, `touchpad`, `media`. A narrower profile keeps applications from mistaking the bridge for a device it is not. |
| `--transport SPEC`  | any bumble transport: `usb:0`, `hci-socket`, `serial:/dev/ttyUSB0`, …                                  |
| `--config PATH`     | a bumble device configuration, e.g. `config/classic1.json`; without it a built-in one is used and the key store goes to `--state-dir` |
| `--connect ADDRESS` | connect as soon as the daemon is up                                                                     |
| `--pair`            | accept a pairing request instead of requiring the device to be bonded                                   |
| `--hat {cw,ccw}`    | which way hat switches count                                                                            |
| `--name NAME`       | the name the device reports to userspace                                                               |

## Waiting for readiness

A supervisor that starts the daemon and immediately pokes the socket gets a
race. `--notification-fd` removes it: once the uinput device exists and the
socket is bound, the daemon writes a single newline to the descriptor and closes
it.

```python
read_fd, write_fd = os.pipe()
os.set_inheritable(write_fd, True)  # Python marks new descriptors close-on-exec
process = subprocess.Popen(
    ["buibd", f"--notification-fd={write_fd}", "--socket", "/run/buib/buibd.sock"],
    pass_fds=(write_fd,),
)
os.close(write_fd)  # only the child should hold it now
os.read(read_fd, 1)  # blocks until the daemon is ready
```

The descriptor has to be inheritable — `pass_fds=`, `os.set_inheritable()` or
`FD_CLOEXEC` cleared by hand — otherwise the child never sees it. The newline is
the whole protocol:

* a newline means ready, and the socket is already accepting connections;
* end of file means the daemon died on the way up (a busy socket, no
  `/dev/uinput`), and no notification was sent;
* a blocking `buibctl ping` is then unnecessary.

`--connect` is scheduled but not awaited before the notification: a slow
Bluetooth connection does not hold up readiness, because the uinput device and
the socket are usable while it happens.

## The protocol

One JSON object per line, UTF-8, `\n` terminated, over a `SOCK_STREAM` UNIX
socket. Clients may pipeline requests; answers come back in order.

```json
--> {"command":"connect","args":{"address":"E1:CA:72:48:C4:E8","wait":true}}
<-- {"success":true,"error":null,"data":{"state":"connected","address":"E1:CA:72:48:C4:E8", ...}}
```

| Command      | Arguments              | Answer                                       |
| ------------ | ---------------------- | -------------------------------------------- |
| `ping`       | —                      | `{pong, protocol, version}`                   |
| `status`     | —                      | session, uinput and Bluetooth state           |
| `connect`    | `address`, `wait`, `timeout` | starts an attempt; `wait` holds the reply until it finishes |
| `disconnect` | —                      | drops the link, releases held keys            |
| `paired`     | —                      | `{addresses: [...]}` of bonded devices        |
| `unpair`     | `address`              | `{address, removed}`                          |

## What gets translated

The report descriptor that a device publishes over SDP is parsed at connect
time, and every input report is translated into evdev events:

| HID                                              | evdev                                          |
| ------------------------------------------------ | ---------------------------------------------- |
| Keyboard page (letters, digits, modifiers, F keys) | `KEY_*`                                      |
| Consumer page (media keys)                        | `KEY_PLAYPAUSE`, `KEY_VOLUMEUP`, …            |
| Generic Desktop X/Y/Z in a mouse or pointer      | `REL_X`, `REL_Y`, `REL_WHEEL`                 |
| Generic Desktop X/Y/Z in a game pad, hat switch   | `ABS_X`, `ABS_Y`, … and `BTN_DPAD_*`          |
| Button page, meaning taken from the collection    | `BTN_LEFT` in a mouse, `BTN_SOUTH` in a gamepad |
| Digitizer finger, X/Y, contact identifier        | `BTN_TOUCH` and `ABS_MT_*` (protocol B slots)  |

Limitations, so they are no surprise:

- devices that only speak the **boot protocol** (no report descriptor) are
  rejected with a clear error;
- a touchpad whose contacts are described in a single collection is treated as
  one contact with a slot, which is enough for tap, drag and click but not for
  multi-finger gestures;
- output and feature reports are parsed but not forwarded: buib is a one-way
  bridge.

## Development

```console
$ uv sync --all-packages
$ uv run pytest              # 190+ tests, no hardware needed
$ uv run ruff check .
$ uv run mypy
```

The tests use a fake event sink and a fake Bluetooth controller, so the whole
pipeline — report descriptor, converter, session, IPC, uinput bookkeeping — is
covered without an adapter. `tests/test_daemon.py` in particular runs a real
daemon over a real socket and checks that a failed connection leaves the uinput
device open and usable.

Layout:

```
packages/buib-protocol/      the wire format (standard library only)
packages/buibd/
  src/buibd/cli.py            argument parsing, logging, signals
  src/buibd/daemon.py         wiring and the IPC command handlers
  src/buibd/session.py        session state machine and supervision
  src/buibd/bluetooth.py      the only module that imports bumble
  src/buibd/uinput_device.py  the device, and the keys it is holding down
  src/buibd/ipc.py            the NDJSON server
  src/buibd/hid/              report descriptor parsing and event translation
  src/buibd/profiles.py       uinput capability profiles
packages/buibctl/             the client
packaging/buibd.service       systemd unit
config/classic1.json          sample bumble device configuration
```

