# buib-protocol

The wire format shared by `buibd` (daemon) and `buibctl` (client).

This package is deliberately tiny and depends on the standard library only, so
that installing the client never pulls in a Bluetooth stack.

## Transport

A `SOCK_STREAM` UNIX domain socket speaking newline delimited JSON (NDJSON):
one UTF-8 JSON object per line, terminated by `\n`.

Request (client to daemon):

```json
{"command": "connect", "args": {"address": "E1:CA:72:48:C4:E8"}}
```

Response (daemon to client):

```json
{"success": true, "error": null, "data": {"state": "connecting"}}
```

Fields:

| Request   | Type   | Notes                                            |
| --------- | ------ | ------------------------------------------------ |
| `command` | string | One of [`Command`][buib_protocol.Command].       |
| `args`    | object | Command specific arguments, may be empty.        |

| Response  | Type             | Notes                                     |
| --------- | ---------------- | ----------------------------------------- |
| `success` | bool             | `true` when the command was executed.     |
| `error`   | string \| `null` | Human readable reason when `false`.       |
| `data`    | object           | Command specific result, empty on failure.|

A client may pipeline requests on one connection; responses are emitted in the
order the requests were received.
