"""Command line entry point of the client.

The client only ever imports :mod:`buib_protocol`, which is standard library
only: talking to ``buibd`` must not require a Bluetooth stack, an adapter or
write access to ``/dev/uinput``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from buib_protocol import Client, Command, RemoteError, default_socket_path

__all__ = ["main"]

__version__ = "0.1.0"

#: Exit codes, so that scripts can tell the failures apart.
EXIT_OK = 0
EXIT_DAEMON_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buibctl",
        description="Control buibd, the Bluetooth HID to uinput bridge.",
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        default=None,
        help=f"daemon socket to talk to (default: {default_socket_path()})",
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="how long to wait for a reply (default: 10)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the raw response payload as JSON"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="show the daemon and session state")
    subparsers.add_parser("ping", help="check that the daemon is responsive")

    connect = subparsers.add_parser("connect", help="connect to a Bluetooth HID device")
    connect.add_argument("address", help="Bluetooth address, e.g. E1:CA:72:48:C4:E8")
    connect.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="return as soon as the connection attempt has started",
    )
    connect.add_argument(
        "--wait-timeout",
        metavar="SECONDS",
        type=float,
        default=60.0,
        help="how long --wait waits for the attempt to finish (default: %(default)s)",
    )
    connect.set_defaults(wait=True)

    subparsers.add_parser("disconnect", help="disconnect the current device")
    subparsers.add_parser("paired", help="list devices this daemon has link keys for")

    unpair = subparsers.add_parser("unpair", help="forget a device's link key")
    unpair.add_argument("address", help="Bluetooth address, e.g. E1:CA:72:48:C4:E8")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = _command_for(args)
    try:
        with Client(args.socket, timeout=args.timeout) as client:
            data = client.request(command, **_arguments(args))
    except RemoteError as error:
        print(f"buibctl: {error}", file=sys.stderr)
        return EXIT_DAEMON_ERROR
    except (ConnectionRefusedError, FileNotFoundError) as error:
        _unreachable(args.socket, error)
        return EXIT_UNREACHABLE
    except TimeoutError:
        print("buibctl: the daemon did not answer in time", file=sys.stderr)
        return EXIT_UNREACHABLE
    except OSError as error:
        _unreachable(args.socket, error)
        return EXIT_UNREACHABLE

    if args.json:
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _print_human(args.command, data)
    return EXIT_OK


def _command_for(args: argparse.Namespace) -> Command:
    if args.command == "ping":
        return Command.PING
    if args.command == "status":
        return Command.STATUS
    if args.command == "connect":
        return Command.CONNECT
    if args.command == "disconnect":
        return Command.DISCONNECT
    if args.command == "paired":
        return Command.PAIRED
    return Command.UNPAIR


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "connect":
        return {
            "address": args.address,
            "wait": args.wait,
            "timeout": args.wait_timeout,
        }
    if args.command == "unpair":
        return {"address": args.address}
    return {}


def _unreachable(path: str | None, error: OSError) -> None:
    location = path or default_socket_path()
    hint = ""
    if isinstance(error, FileNotFoundError):
        hint = " (is buibd running?)"
    elif isinstance(error, PermissionError):
        hint = " (is this your socket?)"
    print(f"buibctl: cannot reach buibd at {location}: {error}{hint}", file=sys.stderr)


def _print_human(command: str, data: dict[str, Any]) -> None:
    if command == "ping":
        print(f"buibd {data.get('version', '?')} (protocol {data.get('protocol', '?')})")
        return
    if command == "paired":
        addresses = data.get("addresses") or []
        if not addresses:
            print("no paired devices")
        for address in addresses:
            print(address)
        return
    if command == "unpair":
        if data.get("removed"):
            print(f"forgot {data.get('address')}")
        else:
            print(f"{data.get('address')} was not paired")
        return

    _print_status(data)


def _print_status(data: dict[str, Any]) -> None:
    state = str(data.get("state", "unknown"))
    print(f"state:    {state}")
    if address := data.get("address"):
        print(f"device:   {address}")
    if name := data.get("name"):
        print(f"name:     {name}")
    if connected_for := data.get("connected_for"):
        print(f"for:      {connected_for}s ({data.get('reports', 0)} reports)")
    if error := data.get("error"):
        print(f"error:    {error}")

    if (uinput := data.get("uinput")) and isinstance(uinput, dict):
        print("uinput:")
        print(f"  device:      {uinput.get('name')} [{uinput.get('profile')}]")
        print(f"  capabilities: {uinput.get('capabilities')}")
        if pressed := uinput.get("pressed"):
            print(f"  held down:   {', '.join(pressed)}")
    if isinstance(bluetooth := data.get("bluetooth"), dict):
        state = "open" if bluetooth.get("open") else "closed"
        print(f"bluetooth: {state} ({bluetooth.get('transport')})")
        if bluetooth.get("error"):
            print(f"  error:  {bluetooth['error']}")
    if unsupported := data.get("unsupported"):
        print("unsupported usages:")
        for usage in unsupported:
            print(f"  {usage}")
    if isinstance(device := data.get("device"), dict):
        print("device details:")
        for key, value in device.items():
            if key != "name":
                print(f"  {key.replace('_', ' ')}: {value}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
