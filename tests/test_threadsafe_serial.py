import threading
import time

import pytest
from unittest.mock import MagicMock, patch

import serial

from threadsafe_serial import ThreadSafeSerial


def port_info(device, description="USB Serial"):
    port = MagicMock()
    port.device = device
    port.description = description
    return port


class TestInit:
    def test_default_params(self, serial_manager):
        assert serial_manager.port == "/dev/ttyTEST"
        assert serial_manager.baudrate == 9600
        assert serial_manager.timeout == 0.01

    def test_serial_connected_on_init(self, serial_manager, mock_serial):
        assert serial_manager.serial is mock_serial
        assert serial_manager.is_open
        assert serial_manager.running

    def test_input_buffer_empty_on_init(self, serial_manager):
        assert len(serial_manager.input_buffer) == 0


class TestRead:
    def test_read_all(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello world")
        assert serial_manager.read() == b"hello world"
        assert len(serial_manager.input_buffer) == 0

    def test_read_partial(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello world")
        assert serial_manager.read(5) == b"hello"
        assert bytes(serial_manager.input_buffer) == b" world"

    def test_read_more_than_available(self, serial_manager):
        serial_manager.input_buffer.extend(b"hi")
        assert serial_manager.read(100) == b"hi"
        assert len(serial_manager.input_buffer) == 0

    def test_read_empty_buffer(self, serial_manager):
        assert serial_manager.read() == b""

    def test_read_size_minus_one(self, serial_manager):
        serial_manager.input_buffer.extend(b"data")
        assert serial_manager.read(-1) == b"data"

    def test_read_size_zero(self, serial_manager):
        serial_manager.input_buffer.extend(b"data")
        assert serial_manager.read(0) == b""
        assert bytes(serial_manager.input_buffer) == b"data"

    def test_read_waits_for_data(self, serial_manager, feed):
        threading.Timer(0.05, feed, args=(serial_manager, b"late")).start()
        start = time.monotonic()
        assert serial_manager.read(timeout=5) == b"late"
        assert time.monotonic() - start < 4

    def test_read_waits_for_requested_size(self, serial_manager, feed):
        feed(serial_manager, b"ab")
        threading.Timer(0.05, feed, args=(serial_manager, b"cd")).start()
        assert serial_manager.read(4, timeout=5) == b"abcd"

    def test_read_timeout_returns_what_is_available(self, serial_manager):
        serial_manager.input_buffer.extend(b"ab")
        assert serial_manager.read(5, timeout=0.05) == b"ab"

    def test_blocking_read_returns_on_stop(self, serial_manager):
        threading.Timer(0.05, serial_manager.stop).start()
        assert serial_manager.read(timeout=None) == b""


class TestReadUntil:
    def test_delimiter_found(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello\r\nworld")
        assert serial_manager.read_until(b"\r\n") == b"hello"
        assert bytes(serial_manager.input_buffer) == b"world"

    def test_delimiter_not_found(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello")
        assert serial_manager.read_until(b"\r\n") is None
        assert bytes(serial_manager.input_buffer) == b"hello"

    def test_max_bytes_reached(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello world no delimiter")
        assert serial_manager.read_until(b"\r\n", max_bytes=5) == b"hello"
        assert bytes(serial_manager.input_buffer) == b" world no delimiter"

    def test_delimiter_before_max_bytes(self, serial_manager):
        serial_manager.input_buffer.extend(b"hi\r\nmore data")
        assert serial_manager.read_until(b"\r\n", max_bytes=100) == b"hi"

    def test_max_bytes_caps_line_when_delimiter_is_further_away(self, serial_manager):
        serial_manager.input_buffer.extend(b"A" * 100 + b"\r\n")
        assert serial_manager.read_until(b"\r\n", max_bytes=10) == b"A" * 10
        assert bytes(serial_manager.input_buffer) == b"A" * 90 + b"\r\n"

    def test_delimiter_right_after_max_bytes_ends_the_line(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello\r\n")
        assert serial_manager.read_until(b"\r\n", max_bytes=5) == b"hello"
        assert len(serial_manager.input_buffer) == 0

    def test_max_bytes_waits_until_delimiter_at_boundary_is_ruled_out(self, serial_manager, feed):
        feed(serial_manager, b"hello\r")
        assert serial_manager.read_until(b"\r\n", max_bytes=5) is None
        feed(serial_manager, b"\n")
        assert serial_manager.read_until(b"\r\n", max_bytes=5) == b"hello"
        assert len(serial_manager.input_buffer) == 0

    def test_empty_delimiter_rejected(self, serial_manager):
        with pytest.raises(ValueError):
            serial_manager.read_until(b"")

    def test_non_positive_max_bytes_rejected(self, serial_manager):
        with pytest.raises(ValueError):
            serial_manager.read_until(b"\r\n", max_bytes=0)

    def test_empty_buffer(self, serial_manager):
        assert serial_manager.read_until(b"\r\n") is None

    def test_custom_delimiter(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello|world")
        assert serial_manager.read_until(b"|") == b"hello"
        assert bytes(serial_manager.input_buffer) == b"world"

    def test_partial_delimiter_not_matched(self, serial_manager):
        serial_manager.input_buffer.extend(b"hello\rworld")
        assert serial_manager.read_until(b"\r\n") is None

    def test_delimiter_at_start(self, serial_manager):
        serial_manager.input_buffer.extend(b"\r\ndata")
        assert serial_manager.read_until(b"\r\n") == b""
        assert bytes(serial_manager.input_buffer) == b"data"

    def test_waits_for_delimiter(self, serial_manager, feed):
        feed(serial_manager, b"hel")
        threading.Timer(0.05, feed, args=(serial_manager, b"lo\r\n")).start()
        assert serial_manager.read_until(b"\r\n", timeout=5) == b"hello"

    def test_timeout_returns_none(self, serial_manager):
        serial_manager.input_buffer.extend(b"partial")
        assert serial_manager.read_until(b"\r\n", timeout=0.05) is None
        assert bytes(serial_manager.input_buffer) == b"partial"

    def test_blocking_read_until_returns_on_stop(self, serial_manager):
        threading.Timer(0.05, serial_manager.stop).start()
        assert serial_manager.read_until(b"\r\n", timeout=None) is None


class TestReadline:
    def test_readline_default(self, serial_manager):
        serial_manager.input_buffer.extend(b"line1\r\nline2")
        assert serial_manager.readline() == b"line1"

    def test_readline_custom_terminator(self, serial_manager):
        serial_manager.input_buffer.extend(b"line1\nline2")
        assert serial_manager.readline(terminator=b"\n") == b"line1"

    def test_readline_waits(self, serial_manager, feed):
        threading.Timer(0.05, feed, args=(serial_manager, b"line\r\n")).start()
        assert serial_manager.readline(timeout=5) == b"line"


class TestWrite:
    def test_write_bytes(self, serial_manager, mock_serial):
        serial_manager.write(b"hello")
        mock_serial.write.assert_called_once_with(b"hello")

    def test_write_string_encoded(self, serial_manager, mock_serial):
        serial_manager.write("hello")
        mock_serial.write.assert_called_once_with(b"hello")

    def test_failed_write_raises_and_marks_disconnected(self, serial_manager, mock_serial):
        mock_serial.write.side_effect = serial.SerialException("write failed")
        with pytest.raises(serial.SerialException, match="write failed"):
            serial_manager.write(b"data")
        assert not serial_manager.is_open
        mock_serial.cancel_read.assert_called_once()  # reader thread is woken to reconnect

    def test_write_while_disconnected_raises_without_touching_port(self, serial_manager, mock_serial):
        mock_serial.write.side_effect = serial.SerialException("write failed")
        with pytest.raises(serial.SerialException):
            serial_manager.write(b"first")
        with pytest.raises(serial.SerialException, match="not connected"):
            serial_manager.write(b"second")
        assert mock_serial.write.call_count == 1

    def test_interrupted_write_raises(self, serial_manager, mock_serial):
        mock_serial.write.return_value = 2
        with pytest.raises(serial.SerialException, match="interrupted after 2 of 5 bytes"):
            serial_manager.write(b"hello")
        assert not serial_manager.is_open

    def test_write_after_stop_raises(self, serial_manager, mock_serial):
        serial_manager.stop()
        with pytest.raises(serial.SerialException, match="not connected"):
            serial_manager.write(b"data")
        mock_serial.write.assert_not_called()


class TestWriteLatest:
    def test_write_latest_sets_data(self, serial_manager):
        serial_manager.write_latest(b"cmd1")
        assert serial_manager._latest_write_data == b"cmd1"
        assert serial_manager._latest_write_event.is_set()

    def test_write_latest_replaces_previous(self, serial_manager):
        serial_manager.write_latest(b"cmd1")
        serial_manager.write_latest(b"cmd2")
        assert serial_manager._latest_write_data == b"cmd2"


class TestDetectDevices:
    def test_detect_matching_device(self, serial_manager):
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[port_info("/dev/ttyUSB0")]):
            assert serial_manager.detect_devices() == ["/dev/ttyUSB0"]

    def test_detect_no_matching_device(self, serial_manager):
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[port_info("/dev/ttyS0", "Standard Serial")]):
            assert serial_manager.detect_devices() is None

    def test_detect_multiple_devices(self, serial_manager):
        ports = [port_info("/dev/ttyUSB0"), port_info("/dev/ttyACM0", "ACM Device")]
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=ports):
            assert len(serial_manager.detect_devices()) == 2

    def test_detect_empty_ports(self, serial_manager):
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[]):
            assert serial_manager.detect_devices() is None

    def test_detect_devices_by_device_name(self, serial_manager):
        """detect_devices should match on device path, not just description."""
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[port_info("/dev/ttyACM0", "Generic Serial")]):
            assert serial_manager.detect_devices() == ["/dev/ttyACM0"]


class TestConnect:
    def test_connect_with_explicit_port(self, make_manager, mock_serial):
        mgr = make_manager(port="/dev/ttyTEST")
        assert mgr.serial is mock_serial

    def test_connect_with_auto_detection(self, make_manager):
        with patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[port_info("/dev/ttyUSB0", "USB Device")]):
            mgr = make_manager(port=None, search_pattern=r"USB")
        assert mgr.port == "/dev/ttyUSB0"

    def test_connect_max_retries_exceeded(self):
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial",
                   side_effect=serial.SerialException("fail")), \
             patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[]):
            with pytest.raises(serial.SerialException):
                ThreadSafeSerial(port=None, max_reconnect_attempts=2, timeout=0.01)

    def test_explicit_port_is_retried_instead_of_auto_detecting(self, mock_serial):
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial",
                   side_effect=[serial.SerialException("busy"), serial.SerialException("busy"), mock_serial]
                   ) as opened, \
             patch("threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports",
                   return_value=[port_info("/dev/ttyUSB0")]) as comports, \
             patch.object(ThreadSafeSerial, "_read_serial"), \
             patch.object(ThreadSafeSerial, "_write_serial"):
            mgr = ThreadSafeSerial(port="/dev/ttyUSB1", timeout=0.01)
        assert [c.kwargs["port"] for c in opened.call_args_list] == ["/dev/ttyUSB1"] * 3
        comports.assert_not_called()
        assert mgr.port == "/dev/ttyUSB1"
        mgr.stop()

    def test_auto_detection_prefers_last_connected_device(self, make_manager):
        comports = "threadsafe_serial.threadsafe_serial.serial.tools.list_ports.comports"
        with patch(comports, return_value=[port_info("/dev/ttyUSB1")]):
            mgr = make_manager(port=None)
        assert mgr.port == "/dev/ttyUSB1"
        with patch(comports, return_value=[port_info("/dev/ttyUSB0"), port_info("/dev/ttyUSB1")]), \
             patch("threadsafe_serial.threadsafe_serial.serial.Serial") as opened:
            mgr._open()
        assert opened.call_args.kwargs["port"] == "/dev/ttyUSB1"


class TestReaderThread:
    """The real reader thread on mocked ports: reconnection and shutdown."""

    def test_reconnects_after_read_error(self, new_serial):
        first, second = new_serial(), new_serial()
        first.read.side_effect = serial.SerialException("device disconnected")
        lines = iter([b"hi\n"])
        second.read.side_effect = lambda size=1: next(lines, None) or time.sleep(0.005) or b""
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", side_effect=[first, second]):
            mgr = ThreadSafeSerial(port="/dev/ttyTEST", timeout=0.01)
            try:
                assert mgr.readline(b"\n", timeout=5) == b"hi"
                first.close.assert_called_once()
                assert mgr.serial is second
                assert mgr.is_open and mgr.running
                assert mgr.reader_thread.is_alive() and mgr.writer_thread.is_alive()
            finally:
                mgr.stop()

    def test_write_error_triggers_reconnect(self, new_serial, wait_until):
        first, second = new_serial(), new_serial()
        first.write.side_effect = serial.SerialException("write failed")
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", side_effect=[first, second]):
            mgr = ThreadSafeSerial(port="/dev/ttyTEST", timeout=0.01)
            try:
                with pytest.raises(serial.SerialException):
                    mgr.write(b"lost")
                assert wait_until(lambda: mgr.serial is second and mgr.is_open)
                mgr.write(b"after")
                second.write.assert_called_once_with(b"after")
                assert mgr.reader_thread.is_alive() and mgr.writer_thread.is_alive()
            finally:
                mgr.stop()

    def test_gives_up_after_max_reconnect_attempts(self, new_serial, wait_until):
        first = new_serial()
        first.read.side_effect = serial.SerialException("device disconnected")
        failures = [serial.SerialException("gone")] * 2
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", side_effect=[first, *failures]):
            mgr = ThreadSafeSerial(port="/dev/ttyTEST", timeout=0.01, max_reconnect_attempts=2)
            try:
                assert wait_until(lambda: not mgr.running)
                with pytest.raises(serial.SerialException) as excinfo:
                    mgr.write(b"x")
                assert str(excinfo.value.__cause__) == "gone"
                assert mgr.readline(timeout=None) is None
            finally:
                mgr.stop()
        mgr.reader_thread.join(timeout=5)
        assert not mgr.reader_thread.is_alive()

    def test_stop_does_not_reconnect(self, new_serial):
        ser = new_serial()
        cancelled = threading.Event()
        ser.cancel_read.side_effect = cancelled.set

        def read(size=1):
            # Like a real port closed underneath a read: fail once cancelled
            if cancelled.wait(0.005):
                raise serial.SerialException("read failed: [Errno 9] Bad file descriptor")
            return b"x"

        ser.read.side_effect = read
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", return_value=ser) as opened:
            mgr = ThreadSafeSerial(port="/dev/ttyTEST", timeout=0.01)
            time.sleep(0.05)
            mgr.stop()
        assert opened.call_count == 1
        ser.close.assert_called_once()
        assert not mgr.reader_thread.is_alive()
        assert not mgr.writer_thread.is_alive()
        assert not mgr.running and not mgr.is_open


class TestContextManager:
    def test_enter_returns_self(self, serial_manager):
        assert serial_manager.__enter__() is serial_manager

    def test_exit_stops(self, serial_manager):
        serial_manager.__exit__(None, None, None)
        assert serial_manager.running is False

    def test_with_statement(self, mock_serial):
        with patch("threadsafe_serial.threadsafe_serial.serial.Serial", return_value=mock_serial):
            with ThreadSafeSerial(port="/dev/ttyTEST", timeout=0.01) as mgr:
                assert mgr.serial is mock_serial
        assert mgr.running is False
        assert not mgr.reader_thread.is_alive()
        mock_serial.close.assert_called_once()


class TestProperties:
    def test_in_waiting_empty(self, serial_manager):
        assert serial_manager.in_waiting == 0

    def test_in_waiting_with_data(self, serial_manager):
        serial_manager.input_buffer.extend(b"12345")
        assert serial_manager.in_waiting == 5

    def test_is_open_true(self, serial_manager):
        assert serial_manager.is_open is True

    def test_is_open_false_after_stop(self, serial_manager):
        serial_manager.stop()
        assert serial_manager.is_open is False


class TestStop:
    def test_stop_closes_serial(self, serial_manager, mock_serial):
        serial_manager.stop()
        assert serial_manager.running is False
        mock_serial.close.assert_called_once()

    def test_stop_cancels_blocking_io(self, serial_manager, mock_serial):
        serial_manager.stop()
        mock_serial.cancel_read.assert_called_once()
        mock_serial.cancel_write.assert_called_once()

    def test_stop_is_idempotent(self, serial_manager, mock_serial):
        serial_manager.stop()
        serial_manager.stop()
        mock_serial.close.assert_called_once()

    def test_close_delegates_to_stop(self, serial_manager, mock_serial):
        serial_manager.close()
        assert serial_manager.running is False
        mock_serial.close.assert_called_once()

    def test_read_after_stop_returns_buffered_data(self, serial_manager):
        serial_manager.input_buffer.extend(b"leftover")
        serial_manager.stop()
        assert serial_manager.read() == b"leftover"
