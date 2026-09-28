"""End to end test of the readiness notification, across a real process.

Everything else can be tested in process, but the point of ``--notification-fd``
is a descriptor that a *parent* hands to a *child*, so this runs buibd in a
subprocess and reads the notification through the other end of a pipe.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# buibd needs /dev/uinput, which a test machine usually does not let us have.
# The script below replaces only uinput.Device, so the rest of the startup path
# runs for real: the same event tables, the same profile, the same socket code.
# Importing uinput itself is harmless - it only loads a library, and opens
# /dev/uinput when a device is created.
FAKE_UINPUT = """
import sys

import uinput


class Device:
    \"\"\"Stands in for uinput.Device: records nothing, opens nothing.\"\"\"

    def __init__(self, events, name, bustype, vendor, product, version):
        self.events = list(events)
        self.name = name

    def emit(self, event, value, syn=True):
        pass

    def syn(self):
        pass

    def destroy(self):
        pass


uinput.Device = Device

from buibd.cli import main

sys.exit(main(sys.argv[1:]))
"""


class Started:
    """A buibd subprocess, plus the pipe its readiness notification arrives on."""

    def __init__(
        self, process: subprocess.Popen[bytes], read_fd: int, signalled_fd: int
    ) -> None:
        self.process = process
        self.read_fd = read_fd
        #: The descriptor number the daemon was told to write to.
        self.signalled_fd = signalled_fd

    def wait_for_notification(self, timeout: float = 20.0) -> bytes:
        """Read the notification, or report what the daemon printed instead."""
        deadline = time.monotonic() + timeout
        os.set_blocking(self.read_fd, False)
        while time.monotonic() < deadline:
            try:
                data = os.read(self.read_fd, 64)
            except BlockingIOError:
                if self.process.poll() is not None:
                    output = (
                        self.process.stdout.read().decode() if self.process.stdout else ""
                    )
                    raise AssertionError(f"the daemon exited:\n{output}") from None
                time.sleep(0.02)
                continue
            if data:
                return data
        raise AssertionError("the daemon never signalled readiness")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.read_fd)


def start_daemon(tmp_path: Path, *extra: str, notify: bool = True) -> Started:
    """Start buibd in a subprocess, with a pipe as its notification channel.

    The child gets the *write* end, this process keeps the read end and drops
    its own copy of the write end - so that a read sees end of file when the
    child closes or dies.
    """
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    arguments = [
        sys.executable,
        "-c",
        FAKE_UINPUT,
        "--socket",
        str(tmp_path / "buibd.sock"),
        *extra,
    ]
    if notify:
        arguments.append(f"--notification-fd={write_fd}")
    process = subprocess.Popen(
        arguments,
        pass_fds=(write_fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    os.close(write_fd)
    return Started(process, read_fd, write_fd)


@pytest.fixture
def daemon_process(tmp_path: Path):
    """Start buibd, and make sure it is stopped whatever the test does."""
    started: list[Started] = []

    def start(*extra: str, notify: bool = True) -> Started:
        daemon = start_daemon(tmp_path, *extra, notify=notify)
        started.append(daemon)
        return daemon

    yield start
    for daemon in started:
        if daemon.process.poll() is None:
            daemon.process.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                daemon.process.wait(timeout=10)
            else_unused = None  # noqa: F841
        daemon.close()


def test_the_daemon_signals_readiness_to_its_parent(daemon_process) -> None:
    daemon = daemon_process()
    assert daemon.wait_for_notification() == b"\n"

    # A single newline, then the descriptor is closed: the next read is end of
    # file, which is what tells a supervisor the daemon is done.
    os.set_blocking(daemon.read_fd, False)
    assert os.read(daemon.read_fd, 64) == b""
    assert daemon.process.poll() is None


def test_readiness_means_the_socket_is_already_bound(
    tmp_path: Path, daemon_process
) -> None:
    """The contract: the notification implies the socket is already usable."""
    daemon = daemon_process()
    assert daemon.wait_for_notification() == b"\n"
    assert (tmp_path / "buibd.sock").is_socket()


def test_the_socket_answers_as_soon_as_the_notification_arrives(
    tmp_path: Path, daemon_process
) -> None:
    """The point of the whole exercise: no sleeping, no polling, no retry."""
    daemon = daemon_process()
    assert daemon.wait_for_notification() == b"\n"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "buibctl",
            "--socket",
            str(tmp_path / "buibd.sock"),
            "ping",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "buibd" in result.stdout


def test_no_notification_leaves_the_descriptors_alone(
    tmp_path: Path, daemon_process
) -> None:
    """Without the option the daemon writes to nothing at all."""
    daemon = daemon_process(notify=False)
    try:
        # Wait for the socket instead of for a notification.
        for _ in range(500):
            if (tmp_path / "buibd.sock").exists():
                break
            time.sleep(0.02)
        assert (tmp_path / "buibd.sock").is_socket()
        assert daemon.process.poll() is None
    finally:
        os.close(daemon.read_fd)
        daemon.read_fd = -1


def test_the_daemon_logs_which_descriptor_it_signalled(daemon_process) -> None:
    daemon = daemon_process("--log-level", "debug")
    assert daemon.wait_for_notification() == b"\n"

    daemon.process.send_signal(signal.SIGTERM)
    output = daemon.process.communicate(timeout=10)[0].decode()
    assert "signalled readiness on fd " in output
    # The number in the log is the child's: the kernel remaps inherited
    # descriptors on exec, so it is not the one this process passed.
    assert daemon.signalled_fd > 3
