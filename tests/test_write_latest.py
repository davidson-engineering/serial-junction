"""Tests for the write_latest / _write_serial Event-based write path."""

import threading
import time

import pytest

import serial


# ---------------------------------------------------------------------------
# Basic write_latest behavior
# ---------------------------------------------------------------------------

class TestWriteLatestBasic:
    def test_sets_data_and_event(self, serial_manager):
        serial_manager.write_latest(b"hello")

        assert serial_manager._latest_write_data == b"hello"
        assert serial_manager._latest_write_event.is_set()

    def test_accepts_string(self, serial_manager):
        serial_manager.write_latest("hello")

        assert serial_manager._latest_write_data == "hello"
        assert serial_manager._latest_write_event.is_set()

    def test_accepts_bytes(self, serial_manager):
        serial_manager.write_latest(b"\x00\x01\x02")

        assert serial_manager._latest_write_data == b"\x00\x01\x02"

    def test_replaces_previous_unread_data(self, serial_manager):
        serial_manager.write_latest(b"first")
        serial_manager.write_latest(b"second")
        serial_manager.write_latest(b"third")

        assert serial_manager._latest_write_data == b"third"

    def test_event_stays_set_after_multiple_writes(self, serial_manager):
        serial_manager.write_latest(b"a")
        serial_manager.write_latest(b"b")

        assert serial_manager._latest_write_event.is_set()

    @pytest.mark.parametrize("empty", [b"", ""])
    def test_empty_data(self, serial_manager, empty):
        serial_manager.write_latest(empty)

        assert serial_manager._latest_write_data == empty
        assert serial_manager._latest_write_event.is_set()


# ---------------------------------------------------------------------------
# Live _write_serial thread
# ---------------------------------------------------------------------------

class TestWriteSerialThread:
    """The real _write_serial thread against a mocked port."""

    def test_thread_sends_data(self, make_manager, mock_serial, wait_until):
        mgr = make_manager(writer=True)
        mgr.write_latest(b"live_test")

        assert wait_until(lambda: mock_serial.write.called)
        mock_serial.write.assert_called_once_with(b"live_test")
        assert mgr._latest_write_data is None
        assert not mgr._latest_write_event.is_set()

    def test_thread_encodes_string(self, make_manager, mock_serial, wait_until):
        mgr = make_manager(writer=True)
        mgr.write_latest("hello")

        assert wait_until(lambda: mock_serial.write.called)
        mock_serial.write.assert_called_once_with(b"hello")

    def test_thread_sends_only_latest(self, make_manager, mock_serial, wait_until):
        """Values queued while a write is in flight collapse to the newest one."""
        write_started = threading.Event()
        release = threading.Event()

        def slow_write(data):
            write_started.set()
            release.wait(timeout=5)

        mock_serial.write.side_effect = slow_write
        mgr = make_manager(writer=True)

        mgr.write_latest(b"first")
        assert write_started.wait(timeout=5)
        for i in range(20):
            mgr.write_latest(f"val_{i}".encode())
        release.set()

        assert wait_until(lambda: mock_serial.write.call_count == 2)
        time.sleep(0.2)
        assert [c.args[0] for c in mock_serial.write.call_args_list] == [b"first", b"val_19"]

    def test_pending_value_waits_for_connection(self, make_manager, mock_serial, wait_until):
        mgr = make_manager(writer=True)
        mgr._connected.clear()  # as if the reader thread were reconnecting
        mgr.write_latest(b"cmd")
        time.sleep(0.3)
        mock_serial.write.assert_not_called()

        mgr._connected.set()
        assert wait_until(lambda: mock_serial.write.called)
        mock_serial.write.assert_called_once_with(b"cmd")

    def test_failed_value_is_retried_after_reconnect(self, make_manager, mock_serial, wait_until):
        mock_serial.write.side_effect = [serial.SerialException("port gone"), None]
        mgr = make_manager(writer=True)
        mgr.write_latest(b"cmd")

        assert wait_until(lambda: not mgr.is_open)
        assert wait_until(lambda: mgr._latest_write_data == b"cmd")  # requeued after the failure

        mgr._connected.set()  # as if the reader thread had reconnected
        assert wait_until(lambda: mock_serial.write.call_count == 2)
        assert mock_serial.write.call_args.args[0] == b"cmd"

    def test_newer_value_replaces_failed_one(self, make_manager, mock_serial, wait_until):
        mock_serial.write.side_effect = [serial.SerialException("port gone"), None]
        mgr = make_manager(writer=True)
        mgr.write_latest(b"old")
        assert wait_until(lambda: not mgr.is_open)

        mgr.write_latest(b"new")
        mgr._connected.set()
        assert wait_until(lambda: mock_serial.write.call_count == 2)
        assert mock_serial.write.call_args.args[0] == b"new"

    def test_thread_stops_on_stop(self, make_manager):
        mgr = make_manager(writer=True)
        mgr.stop()
        assert not mgr.writer_thread.is_alive()


# ---------------------------------------------------------------------------
# Concurrent write + write_latest interaction
# ---------------------------------------------------------------------------

class TestWriteAndWriteLatestInteraction:
    """Verify write() and write_latest() don't interfere with each other."""

    def test_blocking_write_during_write_latest(self, serial_manager, mock_serial):
        """Calling write() while write_latest data is pending should not corrupt either."""
        serial_manager.write_latest(b"async_cmd")
        serial_manager.write(b"sync_cmd")

        # Blocking write should have gone through immediately
        mock_serial.write.assert_called_once_with(b"sync_cmd")
        # write_latest data should still be pending
        assert serial_manager._latest_write_data == b"async_cmd"

    def test_write_latest_during_blocking_write(self, serial_manager, mock_serial):
        """write_latest during a slow blocking write should not block."""
        write_started = threading.Event()
        write_release = threading.Event()

        def slow_write(data):
            write_started.set()
            write_release.wait(timeout=2)

        mock_serial.write.side_effect = slow_write

        t = threading.Thread(target=serial_manager.write, args=(b"slow",))
        t.start()

        assert write_started.wait(timeout=1)
        # write() is holding the write lock - write_latest should still return immediately
        start = time.monotonic()
        serial_manager.write_latest(b"fast")
        elapsed = time.monotonic() - start

        assert elapsed < 0.1  # write_latest should be near-instant
        assert serial_manager._latest_write_data == b"fast"

        write_release.set()
        t.join(timeout=2)

    def test_reads_not_blocked_by_slow_write(self, serial_manager, mock_serial, feed):
        """A blocked port write must not hold up buffer reads."""
        write_started = threading.Event()
        write_release = threading.Event()

        def slow_write(data):
            write_started.set()
            write_release.wait(timeout=5)

        mock_serial.write.side_effect = slow_write
        t = threading.Thread(target=serial_manager.write, args=(b"slow",))
        t.start()
        assert write_started.wait(timeout=1)

        feed(serial_manager, b"line\r\n")
        start = time.monotonic()
        assert serial_manager.in_waiting == 6
        assert serial_manager.readline() == b"line"
        assert time.monotonic() - start < 0.1

        write_release.set()
        t.join(timeout=2)
