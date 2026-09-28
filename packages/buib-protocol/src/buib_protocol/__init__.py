"""Wire protocol shared by the ``buibd`` daemon and the ``buibctl`` client.

The protocol is newline delimited JSON (NDJSON) over a ``SOCK_STREAM`` UNIX
domain socket: one UTF-8 encoded JSON object per line, terminated by ``\\n``.

This module intentionally depends on the standard library only so that the
client never has to install a Bluetooth stack.

Example
-------
>>> request = Request(Command.PING)
>>> request.to_line()
'{"command": "ping", "args": {}}\\n'
>>> Response.parse(request.to_line())  # doctest: +ELLIPSIS
Traceback (most recent call last):
...
ProtocolError: ...
"""

from __future__ import annotations

import enum
import json
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "Client",
    "Command",
    "ProtocolError",
    "RemoteError",
    "Request",
    "Response",
    "SessionState",
    "default_socket_path",
]

#: Bumped whenever the meaning of an existing field changes. Clients that see a
#: different version may still work, but must not assume compatibility.
PROTOCOL_VERSION: Final = 1

#: Refuse to read (or accept) a single line longer than this, so that a
#: misbehaving or hostile peer cannot exhaust the daemon's memory.
MAX_MESSAGE_BYTES: Final = 1 << 20

#: Name of the socket created by the daemon inside its runtime directory.
DEFAULT_SOCKET_NAME: Final = "buibd.sock"

_DEFAULT_TIMEOUT: Final = 10.0


class ProtocolError(ValueError):
    """Raised when a peer sends something that is not a valid protocol message."""


class RemoteError(Exception):
    """Raised by [`Client`][buib_protocol.Client] when the daemon returns a failure."""


class Command(str, enum.Enum):  # noqa: UP042 - a str mixin keeps the JSON values readable
    """Commands understood by the daemon."""

    PING = "ping"
    STATUS = "status"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PAIRED = "paired"
    UNPAIR = "unpair"

    def __str__(self) -> str:
        return self.value


class SessionState(str, enum.Enum):  # noqa: UP042 - a str mixin keeps the JSON values readable
    """State of the daemon's Bluetooth HID session."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"

    def __str__(self) -> str:
        return self.value


def default_socket_path() -> str:
    """Return the socket path to use when neither side was given an override.

    ``BUIB_SOCKET_PATH`` wins, then the per user XDG runtime directory, and
    finally the system wide location used by the shipped systemd unit.
    """
    override = os.environ.get("BUIB_SOCKET_PATH")
    if override:
        return override
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return os.path.join(runtime_dir, "buib", DEFAULT_SOCKET_NAME)
    return os.path.join("/run", "buib", DEFAULT_SOCKET_NAME)


def _loads(line: str | bytes) -> dict[str, Any]:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError(f"message is not valid UTF-8: {error}") from error
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"message is not valid JSON: {error}") from error
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


@dataclass(frozen=True, slots=True)
class Request:
    """A single command sent by a client."""

    command: Command
    args: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, line: str | bytes) -> Request:
        """Parse a request, raising [`ProtocolError`][buib_protocol.ProtocolError]."""
        message = _loads(line)
        try:
            command = Command(message["command"])
        except KeyError as error:
            raise ProtocolError("missing 'command' field") from error
        except ValueError as error:
            raise ProtocolError(f"unknown command: {message['command']!r}") from error
        args = message.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ProtocolError("'args' must be a JSON object")
        unknown = set(message) - {"command", "args"}
        if unknown:
            raise ProtocolError(f"unknown field(s): {', '.join(sorted(unknown))}")
        return cls(command=command, args=args)

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command.value, "args": dict(self.args)}

    def to_line(self) -> str:
        """Serialize the request as a single NDJSON line (with the newline)."""
        return json.dumps(self.to_dict(), separators=(",", ":")) + "\n"


@dataclass(frozen=True, slots=True)
class Response:
    """A single reply sent by the daemon."""

    success: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def parse(cls, line: str | bytes) -> Response:
        """Parse a response, raising [`ProtocolError`][buib_protocol.ProtocolError]."""
        message = _loads(line)
        if "success" not in message:
            raise ProtocolError("missing 'success' field")
        success = message["success"]
        if not isinstance(success, bool):
            raise ProtocolError("'success' must be a boolean")
        data = message.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ProtocolError("'data' must be a JSON object")
        error = message.get("error")
        if error is not None and not isinstance(error, str):
            raise ProtocolError("'error' must be a string or null")
        unknown = set(message) - {"success", "data", "error"}
        if unknown:
            raise ProtocolError(f"unknown field(s): {', '.join(sorted(unknown))}")
        return cls(success=success, data=data, error=error)

    @classmethod
    def ok(cls, data: Mapping[str, Any] | None = None) -> Response:
        return cls(success=True, data=dict(data or {}))

    @classmethod
    def fail(cls, error: str) -> Response:
        return cls(success=False, error=error)

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "error": self.error, "data": dict(self.data)}

    def to_line(self) -> str:
        """Serialize the response as a single NDJSON line (with the newline)."""
        return json.dumps(self.to_dict(), separators=(",", ":")) + "\n"


class Client:
    """Blocking NDJSON client for the daemon's UNIX socket.

    The client is intentionally trivial: it opens a connection, writes one
    request per line and reads one response per line. Reuse a single instance
    to issue several commands on the same connection.
    """

    def __init__(
        self, path: str | None = None, timeout: float = _DEFAULT_TIMEOUT
    ) -> None:
        self.path = path or default_socket_path()
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._reader: Any = None

    def connect(self) -> None:
        """Connect to the daemon, raising `OSError` when it is not reachable."""
        if self._socket is not None:
            raise RuntimeError("client is already connected")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        self._reader = sock.makefile("rb")

    def close(self) -> None:
        reader, sock = self._reader, self._socket
        self._reader = self._socket = None
        if reader is not None:
            reader.close()
        if sock is not None:
            sock.close()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def __enter__(self) -> Client:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(self, command: Command | str, **args: Any) -> dict[str, Any]:
        """Send a command and return the ``data`` payload of the response.

        Raises:
          RemoteError: the daemon answered with ``success: false``.
          ProtocolError: the daemon answered with a malformed message.
        """
        if self._socket is None:
            raise RuntimeError("client is not connected")
        request = Request(command=Command(command), args=args)
        try:
            self._socket.sendall(request.to_line().encode("utf-8"))
            line = self._reader.readline(MAX_MESSAGE_BYTES + 1)
        except OSError as error:
            raise RemoteError(f"connection to {self.path} failed: {error}") from error
        if not line:
            raise RemoteError(f"daemon at {self.path} closed the connection")
        response = Response.parse(line)
        if not response.success:
            raise RemoteError(response.error or "command failed")
        return dict(response.data)
