# buibctl

The client half of [buib](../README.md).  It depends on `buib-protocol` and
nothing else, so it installs and runs on a machine that has no Bluetooth stack,
no adapter and no access to `/dev/uinput`.

```console
$ buibctl status
$ buibctl connect E1:CA:72:48:C4:E8
$ buibctl disconnect
$ buibctl paired
$ buibctl unpair E1:CA:72:48:C4:E8
```

`--json` prints the raw response payload of the daemon, which is the same
structure documented in `buib-protocol`.

## Exit codes

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| 0    | the command succeeded                     |
| 1    | the daemon rejected the command           |
| 2    | bad usage (argparse)                      |
| 3    | the daemon could not be reached           |
