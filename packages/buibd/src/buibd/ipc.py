"""The NDJSON server on the daemon's UNIX socket.

One :class:`IpcServer` per daemon.  It knows nothing about Bluetooth: it takes a
handler callable, enforces the framing rules of
:mod:`buib_protocol` and makes sure that a misbehaving client can never take
the daemon down.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import pathlib
import socket
import stat
from collections.abc import Awaitable, Callable

from buib_protocol import MAX_MESSAGE_BYTES, ProtocolError, Request, Response

__all__ = ["IpcServer"]

logger = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[Response]]

#: Only the owner of the socket may talk to the daemon; it can drive the input
#: device of the machine.
DEFAULT_SOCKET_MODE = 0o600
DEFAULT_DIRECTORY_MODE = 0o700


class IpcServer:
    """Serves requests from clients over a UNIX domain socket."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        handler: Handler,
        *,
        socket_mode: int = DEFAULT_SOCKET_MODE,
        directory_mode: int = DEFAULT_DIRECTORY_MODE,
    ) -> None:
        self.path = pathlib.Path(path)
        self._handler = handler
        self._socket_mode = socket_mode
        self._directory_mode = directory_mode
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """Bind the socket, refusing to steal it from a running daemon."""
        if self._server is not None:
            return
        parent = self.path.parent
        parent.mkdir(mode=self._directory_mode, parents=True, exist_ok=True)
        self._remove_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._serve,
            path=str(self.path),
            limit=MAX_MESSAGE_BYTES,
        )
        with contextlib.suppress(OSError):
            os.chmod(self.path, self._socket_mode)
        logger.info("listening on %s", self.path)

    def _remove_stale_socket(self) -> None:
        if not self.path.exists():
            return
        if not stat.S_ISSOCK(self.path.stat().st_mode):
            raise FileExistsError(f"{self.path} exists and is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.path))
        except OSError as error:
            if error.errno in (errno.ECONNREFUSED, errno.ENOENT):
                logger.info("removing the stale socket %s", self.path)
                self.path.unlink(missing_ok=True)
                return
            raise
        else:
            raise FileExistsError(
                f"another daemon is already listening on {self.path}; "
                "stop it first or pass a different --socket path"
            )
        finally:
            probe.close()

    async def stop(self) -> None:
        """Close the socket and wait for the clients to notice."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        self._connections.clear()
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)
        logger.info("stopped listening on %s", self.path)

    async def __aenter__(self) -> IpcServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -- connection handling ----------------------------------------------

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send(writer, Response.fail("message too large"))
                    return
                except ConnectionResetError:
                    return
                if not line:
                    return
                response = await self._dispatch(line)
                if not await self._send(writer, response):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("error while serving a client connection")
        finally:
            if task is not None:
                self._connections.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(self, line: bytes) -> Response:
        try:
            request = Request.parse(line)
        except ProtocolError as error:
            logger.info("rejecting a malformed request: %s", error)
            return Response.fail(f"malformed request: {error}")
        try:
            return await self._handler(request)
        except Exception as error:  # a handler bug must not kill the daemon
            logger.exception("command %s failed", request.command)
            return Response.fail(f"internal error: {type(error).__name__}: {error}")

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, response: Response) -> bool:
        try:
            writer.write(response.to_line().encode("utf-8"))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True
