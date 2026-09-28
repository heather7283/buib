# buibd

The daemon half of [buib](../README.md): it owns the uinput device and the
Bluetooth connection, and serves `buibctl` on a UNIX socket.

## Lifetime rules

* The uinput device is created before anything else and closed last.  No
  Bluetooth code path can close it.
* Bluetooth failures are reported through the `status` command; they never
  terminate the process and never unregister the input device.
* On disconnect the session releases the keys, buttons and contacts it was
  holding down, so nothing gets stuck.

## Modules

| Module                | Responsibility                                    |
| --------------------- | ------------------------------------------------- |
| `buibd.cli`           | argument parsing, logging, signal handling        |
| `buibd.daemon`        | wiring and the IPC command handlers               |
| `buibd.session`       | session state machine and supervision             |
| `buibd.bluetooth`     | the only module that imports `bumble`             |
| `buibd.uinput_device` | the uinput device and the key release bookkeeping |
| `buibd.ipc`           | the NDJSON server                                 |
| `buibd.hid.descriptor`| HID report descriptor parser                      |
| `buibd.hid.converter` | input report to evdev event translation           |
| `buibd.hid.usages`    | HID usage tables and the usage to evdev mapping   |
| `buibd.profiles`      | the uinput capability profiles                    |
