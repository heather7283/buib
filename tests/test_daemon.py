"""End to end tests: a real daemon, a real socket, the real client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import stat
from pathlib import Path

import pytest
from buib_protocol import Client, RemoteError
from buibd.daemon import Daemon, DaemonConfig
from buibd.evdev import EV_KEY
from descriptors import BOOT_KEYBOARD
from fakes import FakeController, FakeEventSink, FakeUinputModule


class Harness:
    """A running daemon plus the doubles it was given."""

    def __init__(
        self, daemon: Daemon, sink: FakeEventSink, controller: FakeController
    ) -> None:
        self.daemon = daemon
        self.sink = sink
        self.controller = controller
        self.path = daemon.config.socket_path

    async def request(self, command: str, **args: object) -> dict:
        """Send one command.

        The client is blocking while the daemon shares this event loop, so the
        call is made from a worker thread - exactly what a separate client
        process would look like from the daemon's side.
        """

        def call() -> dict:
            with Client(self.path, timeout=5) as client:
                return client.request(command, **args)

        return await asyncio.to_thread(call)

    async def raw(self, line: bytes) -> str:
        """Send a line straight down the socket and read the answer."""

        def call() -> str:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(self.path)
                sock.sendall(line)
                return sock.makefile("rb").readline().decode()

        return await asyncio.to_thread(call)

    async def lines(self, payload: bytes) -> list[dict]:
        """Send several requests on one connection and collect the answers."""

        def call() -> list[dict]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(self.path)
                sock.sendall(payload)
                reader = sock.makefile("rb")
                return [
                    json.loads(reader.readline()) for _ in range(payload.count(b"\n"))
                ]

        return await asyncio.to_thread(call)


async def harness_request(instance: Daemon, command: str, **args: object) -> dict:
    """Send one command to a daemon running in this event loop, off the loop."""
    return await asyncio.to_thread(
        lambda: _request_now(instance.config.socket_path, command, **args)
    )


def _request_now(path: str, command: str, **args: object) -> dict:
    with Client(path, timeout=5) as client:
        return client.request(command, **args)


@pytest.fixture
async def daemon(tmp_path: Path):
    """A started daemon with fake hardware, stopped at the end of the test."""
    started: list[Harness] = []

    async def start(**overrides: object) -> Harness:
        sink = FakeEventSink()
        controller = FakeController(BOOT_KEYBOARD, paired=["AA:BB:CC:DD:EE:FF"])
        settings: dict[str, object] = {
            "socket_path": str(tmp_path / "buibd.sock"),
            "profile": "universal",
        }
        settings.update(overrides)
        config = DaemonConfig(**settings)  # type: ignore[arg-type]
        daemon = Daemon(config, sink=sink, controller=controller)
        await daemon.start()
        harness = Harness(daemon, sink, controller)
        started.append(harness)
        return harness

    yield start
    for harness in started:
        await harness.daemon.stop()


# -- the socket itself ------------------------------------------------------


async def test_the_socket_is_private(daemon) -> None:
    harness = await daemon()
    mode = stat.S_IMODE(os.stat(harness.path).st_mode)
    assert mode == 0o600


async def test_the_socket_lives_in_its_own_directory(daemon, tmp_path: Path) -> None:
    harness = await daemon(socket_path=str(tmp_path / "run" / "buibd.sock"))  # type: ignore[arg-type]
    assert (tmp_path / "run").is_dir()
    assert harness.path.endswith("buibd.sock")


async def test_a_second_daemon_refuses_the_socket(daemon) -> None:
    harness = await daemon()
    second = Daemon(
        DaemonConfig(socket_path=harness.path),
        sink=FakeEventSink(),
        controller=FakeController(BOOT_KEYBOARD),
    )
    with pytest.raises(FileExistsError, match="another daemon"):
        await second.start()
    assert not second.server.running


async def test_a_stale_socket_is_replaced(daemon) -> None:
    harness = await daemon()
    await harness.daemon.stop()
    assert not Path(harness.path).exists()

    Path(harness.path).write_text("")  # a leftover file, not a socket
    os.unlink(harness.path)
    os.mkfifo(harness.path)  # something that is not a socket
    second = Daemon(
        DaemonConfig(socket_path=harness.path),
        sink=FakeEventSink(),
        controller=FakeController(BOOT_KEYBOARD),
    )
    with pytest.raises(FileExistsError, match="not a socket"):
        await second.start()


async def test_stopping_removes_the_socket(daemon) -> None:
    harness = await daemon()
    await harness.daemon.stop()
    assert not Path(harness.path).exists()
    with pytest.raises(FileNotFoundError):
        await harness.request("ping")


# -- commands ---------------------------------------------------------------


async def test_ping(daemon) -> None:
    harness = await daemon()
    data = await harness.request("ping")
    assert data["pong"] is True
    assert data["protocol"] >= 1
    assert data["version"]


async def test_status_when_idle(daemon) -> None:
    harness = await daemon()
    data = await harness.request("status")

    assert data["state"] == "disconnected"
    assert data["address"] is None
    assert data["uinput"]["profile"] == "universal"
    assert data["uinput"]["capabilities"] > 0
    assert data["uinput"]["pressed"] == []
    assert data["bluetooth"]["open"] is True
    assert data["daemon"]["pid"] == os.getpid()


async def test_connect_and_disconnect_over_the_socket(daemon) -> None:
    harness = await daemon()
    data = await harness.request("connect", address="e1:ca:72:48:c4:e8", wait=True)
    assert data["state"] == "connected"
    assert data["address"] == "E1:CA:72:48:C4:E8"
    assert data["name"] == "Fake Keyboard"

    # Reports from the device reach the uinput sink.
    harness.controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))
    assert harness.sink.names() == [("KEY_A", 1)]
    assert (await harness.request("status"))["reports"] == 1

    status = await harness.request("disconnect")
    assert status["state"] == "disconnected"

    # A key that was held down when the link dropped is released.
    assert not harness.sink.pressed
    assert not harness.sink.closed


async def test_connect_without_wait(daemon) -> None:
    harness = await daemon()
    data = await harness.request("connect", address="E1:CA:72:48:C4:E8")
    assert data["state"] in ("connecting", "connected")


async def test_connect_requires_an_address(daemon) -> None:
    harness = await daemon()
    with pytest.raises(RemoteError, match="'address' is required"):
        await harness.request("connect")


async def test_connect_rejects_a_bad_timeout(daemon) -> None:
    harness = await daemon()
    with pytest.raises(RemoteError, match="must be a number"):
        await harness.request("connect", address="E1:CA:72:48:C4:E8", timeout="soon")


async def test_a_failed_connection_keeps_the_daemon_alive(daemon) -> None:
    harness = await daemon()
    harness.controller.fail_with = "the device is not answering"

    with pytest.raises(RemoteError, match="not answering"):
        await harness.request("connect", address="E1:CA:72:48:C4:E8", wait=True)

    # The daemon, its socket and its uinput device are all still there.
    assert (await harness.request("status"))["state"] == "disconnected"
    assert not harness.sink.closed
    assert harness.sink.released >= 1

    # And it can still connect to something else afterwards.
    harness.controller.fail_with = None
    again = await harness.request("connect", address="AA:BB:CC:DD:EE:FF", wait=True)
    assert again["state"] == "connected"


async def test_disconnect_without_a_connection(daemon) -> None:
    harness = await daemon()
    with pytest.raises(RemoteError, match="not connected"):
        await harness.request("disconnect")


async def test_connecting_twice(daemon) -> None:
    harness = await daemon()
    await harness.request("connect", address="E1:CA:72:48:C4:E8", wait=True)
    with pytest.raises(RemoteError, match="already connected"):
        await harness.request("connect", address="AA:BB:CC:DD:EE:FF")


async def test_paired_and_unpair(daemon) -> None:
    harness = await daemon()
    assert await harness.request("paired") == {"addresses": ["AA:BB:CC:DD:EE:FF"]}

    data = await harness.request("unpair", address="AA:BB:CC:DD:EE:FF")
    assert data["removed"] is True
    assert await harness.request("paired") == {"addresses": []}
    assert (await harness.request("unpair", address="AA:BB:CC:DD:EE:FF"))[
        "removed"
    ] is False


# -- protocol level ---------------------------------------------------------


async def test_malformed_request_gets_an_error(daemon) -> None:
    harness = await daemon()
    answer = json.loads(await harness.raw(b"this is not json\n"))
    assert answer["success"] is False
    assert "malformed request" in answer["error"]


async def test_unknown_command_gets_an_error(daemon) -> None:
    harness = await daemon()
    answer = json.loads(await harness.raw(b'{"command":"reboot","args":{}}\n'))
    assert answer["success"] is False
    assert "unknown command" in answer["error"]


async def test_pipelined_requests_get_answers_in_order(daemon) -> None:
    harness = await daemon()
    answers = await harness.lines(
        b'{"command":"ping"}\n{"command":"ping"}\n{"command":"status"}\n'
    )
    assert [line["success"] for line in answers] == [True, True, True]
    assert answers[-1]["data"]["state"] == "disconnected"


async def test_an_oversized_message_is_rejected(daemon) -> None:
    """A line past the limit gets one error, and the connection is dropped."""
    harness = await daemon()

    def call() -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(harness.path)
            with contextlib.suppress(BrokenPipeError):
                # The server hangs up as soon as the line is too long.
                sock.sendall(
                    b'{"command":"ping","args":{"x":"' + b"a" * (2 << 20) + b'"}}\n'
                )
            return sock.makefile("rb").readline().decode()

    answer = json.loads(await asyncio.to_thread(call))
    assert answer["success"] is False
    assert "too large" in answer["error"]


async def test_a_client_that_hangs_up_mid_command(daemon) -> None:
    harness = await daemon()

    def call() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(harness.path)
            sock.sendall(b'{"command":"pi')
            sock.close()

    await asyncio.to_thread(call)
    # The daemon is still serving the next client.
    assert (await harness.request("ping"))["pong"] is True


async def test_status_while_connected(daemon) -> None:
    harness = await daemon()
    await harness.request("connect", address="E1:CA:72:48:C4:E8", wait=True)
    harness.controller.send_report(bytes([0x02, 0, 0, 4, 0, 0, 0, 0]))

    data = await harness.request("status")
    assert data["state"] == "connected"
    assert data["connected_for"] >= 0
    assert data["uinput"]["pressed"] == ["KEY_A", "KEY_LEFTSHIFT"]
    assert data["device"]["report_descriptor_bytes"] == len(BOOT_KEYBOARD)


# -- shutdown ---------------------------------------------------------------


async def test_stop_closes_everything_in_order(tmp_path: Path) -> None:
    sink = FakeEventSink()
    controller = FakeController(BOOT_KEYBOARD)
    instance = Daemon(
        DaemonConfig(socket_path=str(tmp_path / "buibd.sock")),
        sink=sink,
        controller=controller,
    )
    await instance.start()
    await instance.stop()

    assert controller.closed
    assert not instance.server.running
    assert not instance.session.busy
    # The sink belongs to whoever created it: the daemon only closes its own.
    assert not sink.closed


async def test_a_daemon_owned_device_is_closed_last(
    tmp_path: Path, fake_uinput: FakeUinputModule
) -> None:
    """With no sink injected, the daemon makes the device and must close it."""
    instance = Daemon(DaemonConfig(socket_path=str(tmp_path / "buibd.sock")))
    await instance.start()
    (created,) = fake_uinput.devices
    assert created.destroyed is False

    await instance.stop()
    assert created.destroyed is True


async def test_run_returns_when_asked_to_stop(daemon) -> None:
    harness = await daemon()
    task = asyncio.ensure_future(harness.daemon.run())
    await asyncio.sleep(0)
    harness.daemon.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert harness.daemon._stopping.is_set()


async def test_startup_can_connect_right_away(daemon) -> None:
    harness = await daemon(connect="E1:CA:72:48:C4:E8")
    for _ in range(100):
        if harness.daemon.state.value == "connected":
            break
        await asyncio.sleep(0.01)
    assert harness.daemon.state.value == "connected"
    assert not harness.sink.closed


async def test_the_real_device_class_drives_a_virtual_keyboard(
    tmp_path: Path, fake_uinput: FakeUinputModule
) -> None:
    """The whole pipeline, with only the kernel replaced.

    Real UinputDevice, real report converter, real session and real socket: the
    events that would reach /dev/uinput are the ones the fake module records.
    """
    controller = FakeController(BOOT_KEYBOARD)
    instance = Daemon(
        DaemonConfig(socket_path=str(tmp_path / "buibd.sock")), controller=controller
    )
    await instance.start()
    (created,) = fake_uinput.devices

    try:
        assert (
            await harness_request(
                instance, "connect", address="E1:CA:72:48:C4:E8", wait=True
            )
        )["state"] == "connected"

        # Press 'a', then let go of it.
        controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))
        assert created.written == [((EV_KEY, 30), 1)]  # KEY_A
        assert created.synced == 1

        controller.send_report(bytes([0, 0, 0, 0, 0, 0, 0, 0]))
        assert created.written[-1] == ((EV_KEY, 30), 0)

        # A disconnect releases whatever is held down, and the device survives.
        await harness_request(instance, "disconnect")
        assert not instance.session.status.error
    finally:
        await instance.stop()

    assert created.destroyed is True
