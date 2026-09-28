"""Tests for the buibctl command line client, run against a real daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from buibctl.cli import EXIT_DAEMON_ERROR, EXIT_OK, EXIT_UNREACHABLE, main
from buibd.daemon import Daemon, DaemonConfig
from descriptors import BOOT_KEYBOARD
from fakes import FakeController, FakeEventSink


@pytest.fixture
async def daemon_path(tmp_path: Path):
    """Start a daemon in a background thread and yield its socket path."""
    import asyncio
    import threading

    socket_path = str(tmp_path / "buibd.sock")
    sink = FakeEventSink()
    controller = FakeController(BOOT_KEYBOARD, paired=["AA:BB:CC:DD:EE:FF"])
    daemon = Daemon(
        DaemonConfig(socket_path=socket_path), sink=sink, controller=controller
    )
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "the daemon did not start"

    try:
        yield socket_path, sink, controller
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    """Run the client in process and return ``(exit code, stdout, stderr)``."""
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- output formatting ------------------------------------------------------


async def test_status_output(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _, _ = daemon_path
    code, out, err = run(capsys, "--socket", path, "status")

    assert code == EXIT_OK
    assert err == ""
    assert "state:    disconnected" in out
    assert "uinput:" in out
    assert "capabilities:" in out
    assert "bluetooth: open (usb:0)" in out


async def test_status_json(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _, _ = daemon_path
    code, out, _ = run(capsys, "--socket", path, "--json", "status")

    assert code == EXIT_OK
    data = json.loads(out)
    assert data["state"] == "disconnected"
    assert data["uinput"]["profile"] == "universal"
    assert data["protocol"] >= 1


async def test_ping_output(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _, _ = daemon_path
    code, out, _ = run(capsys, "--socket", path, "ping")
    assert code == EXIT_OK
    assert out.startswith("buibd ")


async def test_connect_and_disconnect(
    daemon_path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, sink, controller = daemon_path

    code, out, _ = run(capsys, "--socket", path, "connect", "e1:ca:72:48:c4:e8")
    assert code == EXIT_OK
    assert "state:    connected" in out
    assert "E1:CA:72:48:C4:E8" in out

    controller.send_report(bytes([0, 0, 0, 4, 0, 0, 0, 0]))
    code, out, _ = run(capsys, "--socket", path, "status")
    assert "KEY_A" in out  # the key is still held down

    code, out, _ = run(capsys, "--socket", path, "disconnect")
    assert code == EXIT_OK
    assert "state:    disconnected" in out
    assert not sink.pressed


async def test_connect_no_wait(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _, _ = daemon_path
    code, out, _ = run(
        capsys, "--socket", path, "connect", "--no-wait", "E1:CA:72:48:C4:E8"
    )
    assert code == EXIT_OK
    assert "state:" in out


async def test_connect_failure(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, sink, controller = daemon_path
    controller.fail_with = "no route to the device"

    code, _, err = run(capsys, "--socket", path, "connect", "E1:CA:72:48:C4:E8")

    assert code == EXIT_DAEMON_ERROR
    assert "no route to the device" in err
    assert not sink.closed


async def test_paired_and_unpair(daemon_path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _, _ = daemon_path
    code, out, _ = run(capsys, "--socket", path, "paired")
    assert code == EXIT_OK
    assert out.strip() == "AA:BB:CC:DD:EE:FF"

    code, out, _ = run(capsys, "--socket", path, "unpair", "AA:BB:CC:DD:EE:FF")
    assert code == EXIT_OK
    assert "forgot AA:BB:CC:DD:EE:FF" in out

    code, out, _ = run(capsys, "--socket", path, "paired")
    assert out.strip() == "no paired devices"

    code, out, _ = run(capsys, "--socket", path, "unpair", "AA:BB:CC:DD:EE:FF")
    assert "was not paired" in out


# -- failures ---------------------------------------------------------------


def test_missing_daemon(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "--socket", str(tmp_path / "nope.sock"), "status")
    assert code == EXIT_UNREACHABLE
    assert "cannot reach buibd" in err
    assert "is buibd running?" in err


async def test_socket_from_the_environment(
    daemon_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _, _ = daemon_path
    monkeypatch.setenv("BUIB_SOCKET_PATH", path)
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK
    assert "state:" in out


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["reboot"])
    assert exit_info.value.code == 2


def test_connect_needs_an_address(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["connect"])
    assert exit_info.value.code == 2


def test_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for command in ("status", "connect", "disconnect", "paired", "unpair", "ping"):
        assert command in out


# -- the installed entry point ----------------------------------------------


def test_the_console_script_works() -> None:
    """``buibctl`` on PATH talks to a daemon, with no bridge of its own."""
    script = Path(sys.executable).with_name("buibctl")
    result = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 0
    assert "buibctl" in result.stdout


def test_the_console_script_reports_a_missing_daemon(tmp_path: Path) -> None:
    script = Path(sys.executable).with_name("buibctl")
    result = subprocess.run(
        [str(script), "--socket", str(tmp_path / "nope.sock"), "status"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == EXIT_UNREACHABLE
    assert "cannot reach buibd" in result.stderr


def test_the_client_environment_is_small() -> None:
    """The client venv must not contain the Bluetooth stack."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import buibctl.cli, sys; print('\\n'.join(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    modules = set(result.stdout.split())
    assert "buibctl.cli" in modules
    for forbidden in ("bumble", "uinput", "buibd"):
        assert not any(
            module == forbidden or module.startswith(forbidden + ".")
            for module in modules
        )
