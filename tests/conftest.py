"""Shared fixtures and the path setup the tests rely on."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The test helpers import each other by module name, so the directory has to be
# importable without installing anything.
sys.path.insert(0, str(Path(__file__).parent))

from descriptors import BOOT_KEYBOARD
from fakes import FakeController, FakeEventSink, FakeUinputModule


@pytest.fixture
def sink() -> FakeEventSink:
    """An event sink that accepts everything the universal profile allows."""
    return FakeEventSink()


@pytest.fixture
def controller() -> FakeController:
    """A Bluetooth controller that always offers a boot keyboard."""
    return FakeController(BOOT_KEYBOARD)


@pytest.fixture
def fake_uinput(monkeypatch: pytest.MonkeyPatch) -> FakeUinputModule:
    """Stand in for the uinput module, so the device wrapper needs no kernel."""
    module = FakeUinputModule()
    monkeypatch.setitem(sys.modules, "uinput", module)
    return module
