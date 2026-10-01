import time
from unittest.mock import MagicMock, patch

import pytest
import serial

from serial_junction import SerialJunction


def new_mock_serial():
    mock = MagicMock(spec=serial.Serial)
    mock.is_open = True
    mock.in_waiting = 0
    mock.write.return_value = None

    def read(size=1):
        time.sleep(0.005)  # behave like a port with a short timeout instead of spinning
        return b""

    mock.read.side_effect = read
    mock.close.side_effect = lambda: setattr(mock, "is_open", False)
    return mock


@pytest.fixture
def new_serial():
    """Factory for additional mock ports, e.g. the one a reconnect opens."""
    return new_mock_serial


@pytest.fixture
def mock_serial():
    """Create a mock serial.Serial instance."""
    return new_mock_serial()


@pytest.fixture
def make_manager(mock_serial):
    """Build SerialJunction instances on mock_serial, stopped at teardown.

    Background threads are suppressed unless reader/writer is True.
    """
    managers = []

    def make(reader=False, writer=False, **kwargs):
        kwargs.setdefault("port", "/dev/ttyTEST")
        kwargs.setdefault("baudrate", 9600)
        kwargs.setdefault("timeout", 0.01)
        kwargs.setdefault("reconnect_delay", 0.01)
        patches = [patch("serial_junction.junction.serial.Serial", return_value=mock_serial)]
        if not reader:
            patches.append(patch.object(SerialJunction, "_read_serial"))
        if not writer:
            patches.append(patch.object(SerialJunction, "_write_serial"))
        for p in patches:
            p.start()
        try:
            mgr = SerialJunction(**kwargs)
        finally:
            for p in patches:
                p.stop()
        managers.append(mgr)
        return mgr

    yield make
    for mgr in managers:
        mgr.stop()


@pytest.fixture
def serial_manager(make_manager):
    """A SerialJunction on a mocked port with background threads suppressed."""
    return make_manager()


@pytest.fixture
def feed():
    """Append bytes to a manager's input buffer the way the reader thread does."""

    def feed(mgr, data):
        with mgr._buffer_ready:
            mgr._buffer.extend(data)
            mgr._buffer_ready.notify_all()

    return feed


@pytest.fixture
def wait_until():
    """Poll a condition until it holds or the timeout expires; returns the final result."""

    def wait_until(condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    return wait_until
