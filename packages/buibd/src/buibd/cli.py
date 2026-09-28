"""Command line entry point of the daemon."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
from collections.abc import Sequence

from buib_protocol import default_socket_path

from buibd.daemon import DAEMON_VERSION, Daemon, DaemonConfig
from buibd.profiles import DEFAULT_PROFILE, PROFILES
from buibd.uinput_device import UinputError

__all__ = ["main"]

logger = logging.getLogger("buibd")

_LOG_LEVELS = ("debug", "info", "warning", "error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buibd",
        description=(
            "Bluetooth HID to uinput bridge. Creates one virtual input device at "
            "startup and keeps it for as long as it runs, then serves buibctl on a "
            "UNIX socket."
        ),
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        default=None,
        help=f"UNIX socket to listen on (default: {default_socket_path()})",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=pathlib.Path,
        default=None,
        help="bumble device configuration file (default: built in)",
    )
    parser.add_argument(
        "--transport",
        metavar="SPEC",
        default="usb:0",
        help="HCI transport spec, as understood by bumble (default: %(default)s)",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE,
        help="capabilities of the uinput device (default: %(default)s)",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        default=None,
        help="name the uinput device reports (default: buib Bluetooth HID bridge)",
    )
    parser.add_argument(
        "--state-dir",
        metavar="PATH",
        type=pathlib.Path,
        default=None,
        help="where to keep link keys (default: $XDG_STATE_HOME/buib)",
    )
    parser.add_argument(
        "--connect",
        metavar="ADDRESS",
        default=None,
        help="connect to this device at startup",
    )
    parser.add_argument(
        "--connect-timeout",
        metavar="SECONDS",
        type=float,
        default=30.0,
        help="how long to wait for each stage of a connection (default: %(default)s)",
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="accept pairing requests instead of requiring the device to be bonded",
    )
    parser.add_argument(
        "--hat",
        choices=("cw", "ccw"),
        default="cw",
        help="direction hat switches count in (default: %(default)s)",
    )
    parser.add_argument(
        "--log-traceback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="log the full traceback of a Bluetooth failure, not just the message",
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="info",
        help="log verbosity (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"buibd {DAEMON_VERSION}")
    return parser


def _state_dir(argument: pathlib.Path | None) -> pathlib.Path:
    if argument is not None:
        return argument
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return pathlib.Path(base) / "buib"
    return pathlib.Path.home() / ".local" / "state" / "buib"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = DaemonConfig(
        socket_path=args.socket or default_socket_path(),
        device_name=args.name or "buib Bluetooth HID bridge",
        profile=args.profile,
        config_path=args.config,
        transport=args.transport,
        state_dir=_state_dir(args.state_dir),
        connect_timeout=args.connect_timeout,
        pair=args.pair,
        hat_direction=args.hat,
        connect=args.connect,
        log_traceback=args.log_traceback,
    )
    try:
        daemon = Daemon(config)
    except UinputError as error:
        print(f"buibd: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"buibd: {error}", file=sys.stderr)
        return 2
    return asyncio.run(_run(daemon))


async def _run(daemon: Daemon) -> int:
    await daemon.install_signal_handlers()
    try:
        await daemon.run()
    except FileExistsError as error:
        logger.error("%s", error)
        return 1
    except OSError as error:
        logger.error("could not start: %s", error)
        return 1
    finally:
        with contextlib.suppress(Exception):
            await daemon.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
