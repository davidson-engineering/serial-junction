"""The old threadsafe_serial import paths still work, with a DeprecationWarning."""

import importlib
import subprocess
import sys

import pytest

from serial_junction import PacketReader, SerialJunction, WindowedPacketReader
from serial_junction.junction import truncate_data


@pytest.fixture
def fresh_import():
    """Import a module as if for the first time, so its import-time warning fires again."""

    def fresh_import(name):
        for mod in [
            m for m in sys.modules if m == "threadsafe_serial" or m.startswith("threadsafe_serial.")
        ]:
            del sys.modules[mod]
        return importlib.import_module(name)

    return fresh_import


def test_package_alias(fresh_import):
    with pytest.warns(DeprecationWarning, match="renamed to serial_junction"):
        old = fresh_import("threadsafe_serial")
    assert old.ThreadSafeSerial is SerialJunction
    assert old.PacketReader is PacketReader
    assert old.WindowedPacketReader is WindowedPacketReader


def test_submodule_aliases(fresh_import):
    with pytest.warns(DeprecationWarning, match="renamed to serial_junction"):
        old = fresh_import("threadsafe_serial.threadsafe_serial")
    # The package is already loaded, so this import does not warn again
    old_packets = importlib.import_module("threadsafe_serial.packet_reader")
    assert old.ThreadSafeSerial is SerialJunction
    assert old.truncate_data is truncate_data
    assert old_packets.WindowedPacketReader is WindowedPacketReader


def test_warning_points_at_the_importing_line():
    """Shown by default when a script imports the old name (warnings in __main__ are visible)."""
    result = subprocess.run(
        [sys.executable, "-c", "import threadsafe_serial"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "<string>:1: DeprecationWarning: threadsafe_serial has been renamed" in result.stderr
