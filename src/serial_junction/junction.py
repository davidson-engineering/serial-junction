#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Created By  : Matthew Davidson
# Created Date: 2024-11-26
# ----------------------------------------------------------------------------
"""A serial port shared safely between threads."""
# ----------------------------------------------------------------------------
import logging
import re
import threading
from typing import Optional, Union

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


def truncate_data(data: bytes, max_len: int = 30) -> bytes:
    """Truncate data if it exceeds the maximum length."""
    if len(data) > max_len:
        return data[:max_len] + b"..."
    return data


class SerialJunction:
    """A thread-safe serial communication class with a continuous byte stream buffer.

    Provides concurrent read/write access to a serial port with automatic
    device detection and reconnection support.

    The background reader thread is the only thread that opens, closes or
    reconnects the port. A writer that hits an error marks the connection as
    lost and wakes the reader, which then reconnects.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 9600,
        timeout: float = 1,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: float = serial.STOPBITS_ONE,
        max_reconnect_attempts: int = 0,
        search_pattern: str = r"ACM|USB",
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.max_reconnect_attempts = max_reconnect_attempts
        self.search_pattern = search_pattern

        # An explicit port is never swapped for an auto-detected one
        self._configured_port = port
        self.serial: Optional[serial.Serial] = None
        self._error: Optional[serial.SerialException] = None

        # Set by stop() or when reconnection gives up; never cleared
        self._stop = threading.Event()
        # Set while self.serial is open and believed healthy
        self._connected = threading.Event()
        # Serializes port writes and replacement of self.serial
        self._write_lock = threading.Lock()

        # Input buffer as a single continuous byte stream, notified on new data and on stop
        self.input_buffer = bytearray()
        self._buffer_ready = threading.Condition()

        # Non-blocking write state (Event-based pattern)
        self._latest_write_data: Optional[Union[str, bytes]] = None
        self._latest_write_event = threading.Event()
        self._latest_write_lock = threading.Lock()

        # Initialize connection and start threads
        self._connect()
        self.reader_thread = threading.Thread(target=self._read_serial, daemon=True)
        self.reader_thread.start()
        self.writer_thread = threading.Thread(target=self._write_serial, daemon=True)
        self.writer_thread.start()

    def detect_devices(self) -> Optional[list[str]]:
        """Detect available serial devices matching the search pattern."""
        ports = serial.tools.list_ports.comports()
        devices = [
            p.device
            for p in ports
            if re.search(self.search_pattern, p.description)
            or re.search(self.search_pattern, p.device)
        ]
        if not devices:
            logger.warning("No matching devices found for pattern: %s", self.search_pattern)
            return None
        logger.info("Detected devices: %s", devices)
        return devices

    def _open(self) -> serial.Serial:
        """Open the configured port, or a device matching search_pattern."""
        port = self._configured_port
        if port is None:
            devices = self.detect_devices()
            if not devices:
                raise serial.SerialException("No serial devices were detected.")
            # Stay on the device we were last connected to if it is still present
            port = self.port if self.port in devices else devices[0]
            logger.info("Auto-detected device: %s", port)

        ser = serial.Serial(
            port=port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
        )
        self.port = port
        return ser

    def _connect(self) -> bool:
        """Open the port, retrying every `timeout` seconds until it succeeds.

        Returns False if stop() is called first. Raises SerialException once
        max_reconnect_attempts is exhausted (0 means retry forever).
        """
        attempts = 0
        target = self._configured_port or f"device matching {self.search_pattern!r}"
        while not self._stop.is_set():
            try:
                ser = self._open()
            except serial.SerialException as e:
                attempts += 1
                logger.error("Failed to connect to %s: %s", target, e)
                if 0 < self.max_reconnect_attempts <= attempts:
                    logger.error(
                        "Max reconnect attempts (%d) reached for %s",
                        self.max_reconnect_attempts,
                        target,
                    )
                    raise
                self._stop.wait(self.timeout)
                continue

            with self._write_lock:
                self.serial = ser
                self._connected.set()
            logger.info("Connected to %s at %d baud", self.port, self.baudrate)
            return True
        return False

    def _reconnect(self) -> bool:
        """Close the failed port and open a new one.

        Returns False when the reader thread should exit: stop() was called,
        or max_reconnect_attempts ran out (running then reports False).
        """
        old = self.serial
        old.cancel_write()  # Release a write blocked on the dead port
        with self._write_lock:
            try:
                old.close()
            except (serial.SerialException, OSError) as e:
                logger.debug("Error closing %s: %s", self.port, e)

        logger.info("Attempting to reconnect to %s...", self.port)
        try:
            return self._connect()
        except serial.SerialException as e:
            self._error = e
            self._stop.set()
            with self._buffer_ready:
                self._buffer_ready.notify_all()
            return False

    def _read_serial(self) -> None:
        """Continuously read from the serial port into the buffer, reconnecting as needed."""
        while not self._stop.is_set():
            if not self._connected.is_set() and not self._reconnect():
                return
            ser = self.serial
            try:
                # Block until at least one byte arrives (serial timeout; stop() cancels it)
                data = ser.read(1)
                if data:
                    # Read any remaining buffered bytes
                    remaining = ser.in_waiting
                    if remaining:
                        data += ser.read(remaining)
            except (serial.SerialException, OSError) as e:
                if self._stop.is_set():
                    return
                logger.error("Serial read error on %s: %s. Attempting to reconnect", self.port, e)
                self._connected.clear()
                continue

            if data:
                logger.debug(
                    "RECVD %d bytes from %s: %s",
                    len(data),
                    self.port,
                    truncate_data(data),
                )
                with self._buffer_ready:
                    self.input_buffer.extend(data)
                    self._buffer_ready.notify_all()

    def _write_serial(self) -> None:
        """Send the latest write_latest() value whenever the port is connected."""
        while not self._stop.is_set():
            # Pending data stays queued while disconnected and is sent once the port is back
            if not self._latest_write_event.wait(timeout=0.1) or not self._connected.wait(timeout=0.1):
                continue
            with self._latest_write_lock:
                data = self._latest_write_data
                self._latest_write_data = None
                self._latest_write_event.clear()
            if data is None:
                continue
            try:
                self.write(data)
            except serial.SerialException:
                # Retry after reconnecting, unless a newer value has been queued meanwhile
                with self._latest_write_lock:
                    if self._latest_write_data is None:
                        self._latest_write_data = data
                        self._latest_write_event.set()
            except Exception as e:
                logger.error("Unexpected write error on %s: %s", self.port, e)

    def read(self, size: int = -1, timeout: Optional[float] = 0) -> bytes:
        """Thread-safe read of up to `size` bytes from the buffer (-1 for all available).

        Waits up to `timeout` seconds for `size` bytes, or for any data if size
        is -1. 0 returns immediately and None waits until data arrives or the
        connection stops. On timeout, returns whatever is available, possibly b"".
        """
        needed = 1 if size < 0 else size
        with self._buffer_ready:
            self._buffer_ready.wait_for(
                lambda: len(self.input_buffer) >= needed or self._stop.is_set(), timeout
            )
            if size < 0 or size > len(self.input_buffer):
                size = len(self.input_buffer)
            data = bytes(self.input_buffer[:size])
            del self.input_buffer[:size]
        return data

    def read_until(
        self,
        expected: bytes = b"\r\n",
        max_bytes: Optional[int] = None,
        timeout: Optional[float] = 0,
    ) -> Optional[bytes]:
        """Read from the buffer until the expected delimiter is found.

        Returns the data before the delimiter and consumes the delimiter. If
        `max_bytes` is set and no delimiter starts within the first `max_bytes`
        bytes, returns exactly `max_bytes` bytes instead. Waits up to `timeout`
        seconds (None waits indefinitely) and returns None if nothing is ready.
        """
        if not expected:
            raise ValueError("expected must not be empty")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        with self._buffer_ready:
            self._buffer_ready.wait_for(
                lambda: self._find_line(expected, max_bytes) is not None or self._stop.is_set(),
                timeout,
            )
            bounds = self._find_line(expected, max_bytes)
            if bounds is None:
                return None
            end, consumed = bounds
            data = bytes(self.input_buffer[:end])
            del self.input_buffer[:consumed]
        return data

    def _find_line(self, expected: bytes, max_bytes: Optional[int]) -> Optional[tuple[int, int]]:
        """Return (data length, bytes to consume) for the next line, or None if incomplete."""
        if max_bytes is None:
            idx = self.input_buffer.find(expected)
        else:
            # Only a delimiter starting within max_bytes ends the line. Cap the chunk once
            # enough bytes are buffered to rule out a delimiter straddling the boundary.
            idx = self.input_buffer.find(expected, 0, max_bytes + len(expected))
            if idx == -1 and len(self.input_buffer) >= max_bytes + len(expected):
                return max_bytes, max_bytes
        if idx == -1:
            return None
        return idx, idx + len(expected)

    def readline(self, terminator: bytes = b"\r\n", timeout: Optional[float] = 0) -> Optional[bytes]:
        """Read a line from the buffer using the specified terminator."""
        return self.read_until(terminator, timeout=timeout)

    def write(self, data: Union[str, bytes]) -> None:
        """Thread-safe blocking write to the serial port.

        Raises serial.SerialException if the port is not connected or the write
        fails. The data is not retried; the background reader reconnects.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._write_lock:
            if not self._connected.is_set():
                raise serial.SerialException(f"{self.port} is not connected") from self._error
            ser = self.serial
            try:
                written = ser.write(data)
                if written is not None and written < len(data):
                    raise serial.SerialException(
                        f"Write to {self.port} interrupted after {written} of {len(data)} bytes"
                    )
            except serial.SerialException as e:
                if not self._stop.is_set():
                    logger.error("Serial write error on %s: %s. Attempting to reconnect", self.port, e)
                self._connected.clear()
                ser.cancel_read()  # Wake the reader thread so it reconnects
                raise
        logger.debug(
            "SENT %d bytes to %s: %s",
            len(data),
            self.port,
            truncate_data(data),
        )

    def write_latest(self, data: Union[str, bytes]) -> None:
        """Non-blocking write that keeps only the latest command.

        If a command is already queued, it is replaced with the new one.
        This is ideal for real-time control where only the most recent state matters.
        A command queued while disconnected is sent once the port reconnects.
        """
        with self._latest_write_lock:
            self._latest_write_data = data
            self._latest_write_event.set()

    def stop(self) -> None:
        """Stop the background threads and close the port. Safe to call more than once."""
        self._stop.set()
        self._connected.clear()
        ser = self.serial
        ser.cancel_read()
        ser.cancel_write()
        with self._buffer_ready:
            self._buffer_ready.notify_all()

        for thread in (self.reader_thread, self.writer_thread):
            if thread is not threading.current_thread():
                thread.join()

        with self._write_lock:
            if self.serial.is_open:
                self.serial.close()
                logger.info("Serial connection closed for %s", self.port)

    @property
    def running(self) -> bool:
        """False once stop() has been called or reconnection has given up."""
        return not self._stop.is_set()

    @property
    def in_waiting(self) -> int:
        """Get the number of bytes in the input buffer."""
        with self._buffer_ready:
            return len(self.input_buffer)

    @property
    def is_open(self) -> bool:
        """Check if the serial port is currently connected."""
        return self._connected.is_set()

    def __enter__(self) -> "SerialJunction":
        return self

    def __exit__(self, exc_type: type, exc_value: Exception, traceback: object) -> None:
        self.stop()

    def close(self) -> None:
        """Close the serial connection (alias for stop)."""
        self.stop()
