"""End-to-end tests over real pseudo-terminals (POSIX only).

Each FakeDevice holds the master side of a pty, playing the device, and
ThreadSafeSerial opens the slave through pyserial exactly as it would open a
USB adapter. The slave is reached through a symlink, so it has a stable path
like /dev/ttyUSB0, and unplugging closes the pty and removes that path.
"""
import os
import select
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import serial

from threadsafe_serial import ThreadSafeSerial, WindowedPacketReader

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX pseudo-terminals")

PACKET = bytes([0xA5, 0x01, 0x02, 0x03, 0x5A])
PAYLOAD = PACKET[1:-1]


class FakeDevice:
    """A pty whose slave is reachable at a stable path, like /dev/ttyUSB0."""

    def __init__(self, path):
        self.path = str(path)
        self.master = None
        self.plug()

    def plug(self):
        self.master, self._slave = os.openpty()
        os.set_blocking(self.master, False)
        os.symlink(os.ttyname(self._slave), self.path)

    def unplug(self):
        os.unlink(self.path)
        os.close(self.master)
        os.close(self._slave)
        self.master = None

    def close(self):
        if self.master is not None:
            self.unplug()

    def send(self, data):
        os.write(self.master, data)

    def receive(self, size, timeout=5.0):
        """Return what the host wrote, once `size` bytes arrive or timeout expires."""
        data = b""
        deadline = time.monotonic() + timeout
        while len(data) < size and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.02)
            if ready:
                data += os.read(self.master, 65536)
        return data


@pytest.fixture
def device(tmp_path):
    devices = []

    def make(name="ttyUSB0"):
        dev = FakeDevice(tmp_path / name)
        devices.append(dev)
        return dev

    yield make
    for dev in devices:
        dev.close()


@pytest.fixture
def connect():
    """Open ThreadSafeSerial instances that are stopped at teardown."""
    instances = []

    def make(port, **kwargs):
        kwargs.setdefault("timeout", 0.05)
        tss = ThreadSafeSerial(port=port, baudrate=115200, **kwargs)
        instances.append(tss)
        return tss

    yield make
    for tss in instances:
        tss.stop()


def port_info(device):
    port = MagicMock()
    port.device = device
    port.description = "USB Serial"
    return port


def test_threads_survive_unplug_and_replug(device, connect, wait_until):
    dev = device()
    tss = connect(dev.path)
    tss.write_latest(b"before")
    assert dev.receive(6) == b"before"

    dev.unplug()
    assert wait_until(lambda: not tss.is_open)
    dev.plug()
    assert wait_until(lambda: tss.is_open)

    assert tss.reader_thread.is_alive()
    assert tss.writer_thread.is_alive()
    tss.write_latest(b"after")
    assert dev.receive(5) == b"after"
    dev.send(b"hello\r\n")
    assert tss.readline(timeout=5) == b"hello"


def test_value_queued_while_unplugged_is_sent_after_replug(device, connect, wait_until):
    dev = device()
    tss = connect(dev.path)
    dev.unplug()
    assert wait_until(lambda: not tss.is_open)

    tss.write_latest(b"SET_SPEED 100\r\n")
    dev.plug()
    assert dev.receive(15) == b"SET_SPEED 100\r\n"


def test_write_raises_while_unplugged(device, connect, wait_until):
    dev = device()
    tss = connect(dev.path)
    dev.unplug()
    assert wait_until(lambda: not tss.is_open)

    with pytest.raises(serial.SerialException, match="not connected"):
        tss.write(b"ping\r\n")


def test_explicit_port_is_not_swapped_for_another_device(device, connect, wait_until, caplog):
    intended = device("ttyUSB1")
    other = device("ttyUSB0")
    with patch("serial.tools.list_ports.comports", return_value=[port_info(other.path)]):
        tss = connect(intended.path)
        intended.unplug()
        # Let reconnection fail at least once while another matching device is present
        assert wait_until(lambda: "Failed to connect" in caplog.text)
        intended.plug()
        assert wait_until(lambda: tss.is_open)

    assert tss.port == intended.path
    tss.write(b"MOTOR SPEED 100\r\n")
    assert intended.receive(17) == b"MOTOR SPEED 100\r\n"
    assert other.receive(1, timeout=0.3) == b""


def test_auto_detected_device_reconnects(device, connect, wait_until):
    dev = device()
    with patch("serial.tools.list_ports.comports", return_value=[port_info(dev.path)]):
        tss = connect(None)
        assert tss.port == dev.path
        dev.unplug()
        assert wait_until(lambda: not tss.is_open)
        dev.plug()
        assert wait_until(lambda: tss.is_open)

    dev.send(b"back\r\n")
    assert tss.readline(timeout=5) == b"back"


def test_gives_up_after_max_reconnect_attempts(device, connect, wait_until):
    dev = device()
    tss = connect(dev.path, max_reconnect_attempts=2)
    dev.unplug()

    assert wait_until(lambda: not tss.running)
    with pytest.raises(serial.SerialException) as excinfo:
        tss.write(b"x")
    assert isinstance(excinfo.value.__cause__, serial.SerialException)
    assert tss.readline(timeout=None) is None  # blocked readers are released, not hung


def test_stop_under_traffic_keeps_port_closed(device):
    for i in range(20):
        dev = device(f"tty{i}")
        flowing = threading.Event()
        flowing.set()

        def traffic():
            while flowing.is_set():
                try:
                    dev.send(b"x" * 64)
                except BlockingIOError:
                    pass
                except OSError:
                    return
                time.sleep(0.0005)

        feeder = threading.Thread(target=traffic)
        feeder.start()
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", wraps=serial.Serial) as opened:
            tss = ThreadSafeSerial(port=dev.path, baudrate=115200, timeout=0.05)
            time.sleep(0.02)
            tss.stop()
        flowing.clear()
        feeder.join(timeout=5)

        assert opened.call_count == 1  # stop() must never trigger a reconnect
        assert not tss.running
        assert not tss.is_open
        assert not tss.serial.is_open
        assert not tss.reader_thread.is_alive()
        assert not tss.writer_thread.is_alive()
        dev.close()


def test_blocked_write_does_not_stall_reads(device, connect):
    dev = device()
    tss = connect(dev.path)
    errors = []

    def big_write():
        try:
            tss.write(b"x" * 2_000_000)  # the device never drains this, so it blocks
        except serial.SerialException as e:
            errors.append(e)

    writer = threading.Thread(target=big_write)
    writer.start()
    time.sleep(0.2)
    assert writer.is_alive()

    dev.send(b"ping\r\n")
    assert tss.readline(timeout=5) == b"ping"
    assert tss.in_waiting == 0

    tss.stop()  # cancels the blocked write
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert len(errors) == 1 and "interrupted" in str(errors[0])


def test_blocking_readline_does_not_spin(device, connect):
    dev = device()
    tss = connect(dev.path)
    result = {}

    def reader():
        start = time.thread_time()
        result["line"] = tss.readline(timeout=5)
        result["cpu"] = time.thread_time() - start

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.5)
    dev.send(b"hello\r\n")
    t.join(timeout=5)

    assert result["line"] == b"hello"
    assert result["cpu"] < 0.1


def test_read_until_max_bytes_caps_long_lines(device, connect):
    dev = device()
    tss = connect(dev.path)
    dev.send(b"A" * 100 + b"\r\n")

    assert tss.read_until(b"\r\n", max_bytes=10, timeout=5) == b"A" * 10
    assert tss.read(timeout=5)  # remainder is still buffered


def test_packet_reader_with_real_reads(device, connect):
    dev = device()
    tss = connect(dev.path)
    reader = WindowedPacketReader(read_callback=tss.read, window_size=5, timeout=2)

    dev.send(PACKET + PACKET)
    assert reader.read_packet() == PAYLOAD
    assert reader.read_packet() == PAYLOAD

    dev.send(PACKET + PACKET[:2])
    assert reader.read_packet() == PAYLOAD
    dev.send(PACKET[2:])
    assert reader.read_packet() == PAYLOAD

    reader.timeout = 0.5
    start = time.thread_time()
    assert reader.read_packet() is None
    assert time.thread_time() - start < 0.2
