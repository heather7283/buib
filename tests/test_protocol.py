"""Tests for the wire protocol shared by the daemon and the client."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import buib_protocol
import buibctl
import buibctl.cli
import pytest
from buib_protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    Client,
    Command,
    ProtocolError,
    RemoteError,
    Request,
    Response,
    SessionState,
    default_socket_path,
)

# -- enums ------------------------------------------------------------------


def test_commands_and_states_are_strings() -> None:
    assert Command.CONNECT == "connect"
    assert str(Command.PING) == "ping"
    assert Command("status") is Command.STATUS
    assert SessionState("connected") is SessionState.CONNECTED
    assert str(SessionState.DISCONNECTED) == "disconnected"


# -- request ----------------------------------------------------------------


def test_request_round_trip() -> None:
    request = Request(Command.CONNECT, {"address": "E1:CA:72:48:C4:E8", "wait": True})
    line = request.to_line()

    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == {
        "command": "connect",
        "args": {"address": "E1:CA:72:48:C4:E8", "wait": True},
    }
    assert Request.parse(line) == request


def test_request_without_args() -> None:
    assert Request(Command.PING).to_line() == '{"command":"ping","args":{}}\n'
    assert Request.parse('{"command":"ping"}').args == {}
    assert Request.parse('{"command":"ping","args":null}').args == {}


def test_request_accepts_bytes() -> None:
    assert Request.parse(b'{"command":"status"}').command is Command.STATUS


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('{"command":"nope"}', "unknown command"),
        ('{"args":{}}', "missing 'command'"),
        ('{"command":"ping","args":[]}', "'args' must be a JSON object"),
        ('{"command":"ping","extra":1}', "unknown field"),
        ("not json", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('{"command":"ping","args":{', "not valid JSON"),
    ],
)
def test_malformed_requests_are_rejected(line: str, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        Request.parse(line)


def test_oversized_message_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        Request.parse(
            b'{"command":"ping","args":{"x":"' + b"a" * MAX_MESSAGE_BYTES + b'"}}'
        )


# -- response ---------------------------------------------------------------


def test_response_round_trip() -> None:
    response = Response.ok({"state": "connected", "reports": 12})
    assert response.to_line() == (
        '{"success":true,"error":null,"data":{"state":"connected","reports":12}}\n'
    )
    assert Response.parse(response.to_line()) == response


def test_failure_response() -> None:
    response = Response.fail("not connected to any device")
    assert json.loads(response.to_line()) == {
        "success": False,
        "error": "not connected to any device",
        "data": {},
    }
    assert Response.parse(response.to_line()) == response


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('{"error":"x"}', "missing 'success'"),
        ('{"success":"yes"}', "must be a boolean"),
        ('{"success":true,"data":3}', "'data' must be a JSON object"),
        ('{"success":true,"error":3}', "'error' must be a string"),
        ('{"success":true,"what":1}', "unknown field"),
    ],
)
def test_malformed_responses_are_rejected(line: str, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        Response.parse(line)


# -- socket paths -----------------------------------------------------------


def test_socket_path_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUIB_SOCKET_PATH", "/tmp/explicit.sock")
    assert default_socket_path() == "/tmp/explicit.sock"


def test_socket_path_falls_back_to_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUIB_SOCKET_PATH", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert default_socket_path() == "/run/user/1000/buib/buibd.sock"


def test_socket_path_falls_back_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUIB_SOCKET_PATH", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_socket_path() == "/run/buib/buibd.sock"


# -- client -----------------------------------------------------------------


class EchoServer:
    """A one-shot server that answers every request with a canned payload."""

    def __init__(
        self, path: Path, *, fail_with: str | None = None, close: bool = False
    ) -> None:
        self.path = path
        self.fail_with = fail_with
        self.close = close
        self.received: list[Request] = []
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(path))
        self._socket.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._socket.accept()
        with connection:
            for line in connection.makefile("rb"):
                self.received.append(Request.parse(line))
                if self.close:
                    connection.close()
                    return
                response = (
                    Response.fail(self.fail_with)
                    if self.fail_with
                    else Response.ok(
                        {"state": "connected", "reports": len(self.received)}
                    )
                )
                connection.sendall(response.to_line().encode())

    def join(self) -> None:
        self._thread.join(timeout=5)


def test_client_requests_and_parses(tmp_path: Path) -> None:
    server = EchoServer(tmp_path / "s.sock")
    with Client(str(server.path), timeout=5) as client:
        assert client.connected
        assert client.request(Command.STATUS)["state"] == "connected"
        assert client.request("ping")["reports"] == 2
    assert [r.command for r in server.received] == [Command.STATUS, Command.PING]
    server.join()


def test_client_raises_on_failure(tmp_path: Path) -> None:
    server = EchoServer(tmp_path / "s.sock", fail_with="already connected")
    with (
        Client(str(server.path), timeout=5) as client,
        pytest.raises(RemoteError, match="already"),
    ):
        client.request(Command.CONNECT, address="AA:BB:CC:DD:EE:FF")
    server.join()


def test_client_reports_a_closed_connection(tmp_path: Path) -> None:
    server = EchoServer(tmp_path / "s.sock", close=True)
    with (
        Client(str(server.path), timeout=5) as client,
        pytest.raises(RemoteError, match="closed"),
    ):
        client.request(Command.PING)
    server.join()


def test_client_refuses_to_connect_twice(tmp_path: Path) -> None:
    server = EchoServer(tmp_path / "s.sock")
    client = Client(str(server.path), timeout=5)
    client.connect()
    try:
        with pytest.raises(RuntimeError, match="already connected"):
            client.connect()
    finally:
        client.close()
        server.join()


def test_client_without_a_connection() -> None:
    client = Client("/nowhere.sock")
    with pytest.raises(RuntimeError, match="not connected"):
        client.request(Command.PING)


def test_client_reports_a_missing_socket(tmp_path: Path) -> None:
    client = Client(str(tmp_path / "missing.sock"), timeout=1)
    with pytest.raises(FileNotFoundError):
        client.connect()


def test_client_does_not_need_a_bluetooth_stack() -> None:
    """Importing the client must not pull in bumble, uinput or asyncio."""
    import ast

    forbidden = {"bumble", "uinput", "asyncio", "buibd"}
    for module in (buibctl, buibctl.cli, buib_protocol):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not roots & forbidden, f"{module.__name__} imports {roots & forbidden}"

    # The daemon side is the one that is allowed (and expected) to do so.
    import buibd.daemon  # noqa: F401


def test_protocol_version_is_exposed() -> None:
    assert PROTOCOL_VERSION >= 1
