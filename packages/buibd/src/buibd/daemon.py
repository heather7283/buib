"""The buibd daemon: wires the uinput device, the Bluetooth controller, the HID
session and the IPC server together, and implements the IPC commands.

Startup order is deliberate:

1. create the uinput device (if this fails, the daemon does not start at all),
2. create the IPC server,
3. optionally start a connection attempt.

Bluetooth is opened lazily, so a daemon whose adapter is missing (or unplugged
later) keeps running and keeps its socket; only the Bluetooth part of the status
says what went wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buib_protocol import (
    PROTOCOL_VERSION,
    Command,
    Request,
    Response,
    SessionState,
    default_socket_path,
)

from buibd.bluetooth import BluetoothController, BluetoothError
from buibd.evdev import Event, EventSink
from buibd.ipc import IpcServer
from buibd.profiles import DEFAULT_PROFILE, PROFILES
from buibd.session import HidSession, SessionError
from buibd.uinput_device import UinputDevice, describe_event

__all__ = ["Daemon", "DaemonConfig"]

logger = logging.getLogger(__name__)

DAEMON_VERSION = "0.1.0"


@dataclass(slots=True)
class DaemonConfig:
    """Everything the daemon needs to know, all of it fixed at startup."""

    socket_path: str = field(default_factory=default_socket_path)
    device_name: str = "buib Bluetooth HID bridge"
    profile: str = DEFAULT_PROFILE
    config_path: Path | None = None
    transport: str = "usb:0"
    state_dir: Path | None = None
    connect_timeout: float = 30.0
    pair: bool = False
    hat_direction: str = "cw"
    connect: str | None = None

    def validate(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(
                f"unknown profile {self.profile!r}, expected one of {', '.join(PROFILES)}"
            )


class Daemon:
    """The long lived process behind ``buibctl``."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        sink: EventSink | None = None,
        controller: Any | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.started_at = time.time()
        # The uinput device is created first and closed last; see the module
        # docstring.  A test can pass its own sink and controller.
        self._owns_sink = sink is None
        self._sink: Any = (
            sink
            if sink is not None
            else UinputDevice(name=config.device_name, profile=config.profile)
        )
        self._controller: Any = (
            controller
            if controller is not None
            else BluetoothController(
                config_path=config.config_path,
                transport_spec=config.transport,
                state_dir=config.state_dir,
                connect_timeout=config.connect_timeout,
                pair=config.pair,
            )
        )
        self._session = HidSession(
            self._sink, self._controller, on_change=self._log_state
        )
        self._server = IpcServer(config.socket_path, self.handle)
        self._stopping = asyncio.Event()
        self._startup_task: asyncio.Task[None] | None = None

    # -- properties -------------------------------------------------------

    @property
    def session(self) -> HidSession:
        return self._session

    @property
    def server(self) -> IpcServer:
        return self._server

    @property
    def state(self) -> SessionState:
        return self._session.status.state

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Bring up the IPC server and, if asked, the first connection."""
        await self._server.start()
        logger.info(
            "buibd %s ready (pid %d, socket %s)",
            DAEMON_VERSION,
            os.getpid(),
            self.config.socket_path,
        )
        if self.config.connect:
            # Do not hold up startup: connecting may take a while, and the
            # uinput device and socket are already usable.
            self._startup_task = asyncio.create_task(
                self._initial_connect(self.config.connect), name="buibd-startup-connect"
            )

    async def _initial_connect(self, address: str) -> None:
        try:
            await self._session.connect(address)
        except SessionError as error:
            logger.error("could not connect to %s: %s", address, error)

    async def run(self) -> None:
        """Run until a stop is requested."""
        await self.start()
        await self._stopping.wait()

    def request_stop(self) -> None:
        """Ask the daemon to wind down; safe to call from a signal handler."""
        logger.info("shutting down")
        self._stopping.set()

    async def stop(self) -> None:
        """Shut everything down, in the reverse order of startup."""
        self.request_stop()
        if self._startup_task is not None:
            self._startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._startup_task
        with contextlib.suppress(Exception):
            await self._server.stop()
        with contextlib.suppress(Exception):
            await self._session.close()
        with contextlib.suppress(Exception):
            await self._controller.close()
        if self._owns_sink:
            with contextlib.suppress(Exception):
                self._sink.close()
        logger.info("buibd stopped")

    async def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGINT/SIGTERM, as systemd expects."""
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal_number, self.request_stop)

    def _log_state(self) -> None:
        status = self._session.status
        logger.debug("session state: %s", status.state.value)

    # -- IPC --------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        """Dispatch one request.  Never raises."""
        try:
            match request.command:
                case Command.PING:
                    return Response.ok(
                        {
                            "pong": True,
                            "protocol": PROTOCOL_VERSION,
                            "version": DAEMON_VERSION,
                        }
                    )
                case Command.STATUS:
                    return Response.ok(self._status_data())
                case Command.CONNECT:
                    return await self._connect(request)
                case Command.DISCONNECT:
                    await self._session.disconnect()
                    return Response.ok(self._status_data())
                case Command.PAIRED:
                    return Response.ok(
                        {"addresses": await self._controller.paired_devices()}
                    )
                case Command.UNPAIR:
                    return await self._unpair(request)
                case _:  # pragma: no cover - Command is exhaustive
                    return Response.fail(f"unsupported command {request.command}")
        except (SessionError, BluetoothError) as error:
            return Response.fail(str(error))
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("command %s failed", request.command)
            return Response.fail(f"internal error: {type(error).__name__}: {error}")

    async def _connect(self, request: Request) -> Response:
        address = _required_str(request, "address")
        wait = bool(request.args.get("wait", False))
        timeout = request.args.get("timeout", self.config.connect_timeout)
        if not isinstance(timeout, int | float):
            raise SessionError("'timeout' must be a number")
        await self._session.connect(address, wait=wait, timeout=float(timeout))
        return Response.ok(self._status_data())

    async def _unpair(self, request: Request) -> Response:
        address = _required_str(request, "address")
        removed = await self._controller.unpair(address)
        return Response.ok(
            {**self._status_data(), "address": address.upper(), "removed": removed}
        )

    def _status_data(self) -> dict[str, Any]:
        sink = self._sink
        return {
            **self._session.status.to_dict(),
            "protocol": PROTOCOL_VERSION,
            "daemon": {
                "version": DAEMON_VERSION,
                "pid": os.getpid(),
                "started_at": round(self.started_at, 3),
                "uptime": round(time.time() - self.started_at, 1),
                "python": platform.python_version(),
            },
            "uinput": {
                "name": getattr(sink, "name", None),
                "profile": getattr(sink, "profile", None),
                "capabilities": len(getattr(sink, "capabilities", ())),
                "pressed": sorted(_event_names(getattr(sink, "pressed", frozenset()))),
            },
            "bluetooth": {
                "open": bool(getattr(self._controller, "is_open", False)),
                "transport": self.config.transport,
                "controller": getattr(self._controller, "local_name", None),
                "error": getattr(self._controller, "last_error", None),
            },
        }


def _required_str(request: Request, name: str) -> str:
    value = request.args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SessionError(f"'{name}' is required and must be a non-empty string")
    return value.strip()


def _event_names(events: frozenset[Event]) -> list[str]:
    return [describe_event(event) for event in events]
