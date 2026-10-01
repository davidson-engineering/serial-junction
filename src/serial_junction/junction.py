"""A serial port shared safely between threads."""

from __future__ import annotations

import logging
import re
import threading
from types import TracebackType

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


def truncate_data(data: bytes, max_len: int = 30) -> bytes:
    """Truncate data if it exceeds the maximum length."""
    if len(data) > max_len:
        return data[:max_len] + b"..."
    return data


class SerialJunction:
    """One serial port shared safely between threads.

    A background reader thread moves every received byte into a single
    buffer, which any thread can read from with `read`, `read_until` or
    `readline`. Any thread can `write`, and writes are serialized. When the
    device disconnects, the reader thread reopens the port automatically.

    The constructor blocks until the port opens. With the default
    `max_reconnect_attempts=0` it keeps retrying until a device appears.

    Example:
        >>> with SerialJunction("/dev/ttyUSB0", 115200) as junction:  # doctest: +SKIP
        ...     junction.write(b"PING\\r\\n")
        ...     reply = junction.readline(timeout=1.0)
    """

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 9600,
        *,
        timeout: float | None = 1,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: float = serial.STOPBITS_ONE,
        max_reconnect_attempts: int = 0,
        reconnect_delay: float = 1.0,
        search_pattern: str = r"ACM|USB",
    ) -> None:
        """Open the port and start the background threads.

        Args:
            port: Device to open, e.g. "/dev/ttyUSB0" or "COM3". None opens the
                first device whose path or description matches `search_pattern`.
            baudrate: Baud rate.
            timeout: Read timeout of the background reader thread, in seconds,
                or None to block. Rarely needs changing; it does not affect
                `read` and friends, which take their own timeout.
            bytesize: Number of data bits, as for `serial.Serial`.
            parity: Parity checking, as for `serial.Serial`.
            stopbits: Number of stop bits, as for `serial.Serial`.
            max_reconnect_attempts: Failed attempts allowed before giving up on
                a connection, at startup or after a disconnect. 0 retries forever.
            reconnect_delay: Seconds to wait between connection attempts.
            search_pattern: Regular expression used to auto-detect a device
                when `port` is None.

        Raises:
            ValueError: If an argument is out of range.
            serial.SerialException: If the port cannot be opened within
                `max_reconnect_attempts` attempts.
        """
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive, or None to block")
        if max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts must be 0 (retry forever) or more")
        if reconnect_delay < 0:
            raise ValueError("reconnect_delay must not be negative")

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self.search_pattern = search_pattern

        # An explicit port is never swapped for an auto-detected one
        self._configured_port = port
        self._serial: serial.Serial
        self._error: serial.SerialException | None = None

        # Set by stop() or when reconnection gives up; never cleared
        self._stop = threading.Event()
        # Set while the port is open and believed healthy
        self._connected = threading.Event()
        # Serializes port writes and replacement of the port
        self._write_lock = threading.Lock()

        # Received bytes as one continuous stream, notified on new data and on stop
        self._buffer = bytearray()
        self._buffer_ready = threading.Condition()

        # Latest-value write state for write_latest()
        self._latest_write_data: str | bytes | None = None
        self._latest_write_event = threading.Event()
        self._latest_write_lock = threading.Lock()

        self._connect()
        self._reader_thread = threading.Thread(
            target=self._read_serial, name=f"serial-junction-reader:{self.port}", daemon=True
        )
        self._reader_thread.start()
        self._writer_thread = threading.Thread(
            target=self._write_serial, name=f"serial-junction-writer:{self.port}", daemon=True
        )
        self._writer_thread.start()

    def __repr__(self) -> str:
        if self._stop.is_set():
            state = "stopped"
        elif self._connected.is_set():
            state = "connected"
        else:
            state = "reconnecting"
        return f"<{type(self).__name__} port={self.port!r} baudrate={self.baudrate} {state}>"

    def detect_devices(self) -> list[str]:
        """Return the serial devices whose path or description matches `search_pattern`."""
        devices = [
            p.device
            for p in serial.tools.list_ports.comports()
            if re.search(self.search_pattern, p.description)
            or re.search(self.search_pattern, p.device)
        ]
        if devices:
            logger.info("Detected devices: %s", devices)
        else:
            logger.warning("No matching devices found for pattern: %s", self.search_pattern)
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
        """Open the port, retrying every `reconnect_delay` seconds until it succeeds.

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
                self._stop.wait(self.reconnect_delay)
                continue

            with self._write_lock:
                self._serial = ser
                self._connected.set()
            logger.info("Connected to %s at %d baud", self.port, self.baudrate)
            return True
        return False

    def _reconnect(self) -> bool:
        """Close the failed port and open a new one.

        Returns False when the reader thread should exit: stop() was called,
        or max_reconnect_attempts ran out (running then reports False).
        """
        old = self._serial
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
            ser = self._serial
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
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "RECVD %d bytes from %s: %s", len(data), self.port, truncate_data(data)
                    )
                with self._buffer_ready:
                    self._buffer.extend(data)
                    self._buffer_ready.notify_all()

    def _write_serial(self) -> None:
        """Send the latest write_latest() value whenever the port is connected."""
        while not self._stop.is_set():
            # Pending data stays queued while disconnected and is sent once the port is back
            if not self._latest_write_event.wait(timeout=0.1) or not self._connected.wait(
                timeout=0.1
            ):
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

    def read(self, size: int = -1, timeout: float | None = 0) -> bytes:
        """Read up to `size` bytes from the receive buffer.

        Args:
            size: Maximum number of bytes to return; -1 returns everything buffered.
            timeout: Seconds to wait for `size` bytes, or for any data if `size`
                is -1. 0 returns immediately; None waits until data arrives or
                the junction stops.

        Returns:
            The bytes read. On timeout, whatever is available, possibly b"".
        """
        needed = 1 if size < 0 else size
        with self._buffer_ready:
            self._buffer_ready.wait_for(
                lambda: len(self._buffer) >= needed or self._stop.is_set(), timeout
            )
            if size < 0 or size > len(self._buffer):
                size = len(self._buffer)
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
        return data

    def read_until(
        self,
        expected: bytes = b"\r\n",
        max_bytes: int | None = None,
        timeout: float | None = 0,
    ) -> bytes | None:
        """Read from the receive buffer up to a delimiter.

        Args:
            expected: Delimiter to look for. It is consumed but not returned.
            max_bytes: If set, a line longer than this is returned in pieces of
                exactly `max_bytes` bytes, so one runaway line cannot grow forever.
            timeout: Seconds to wait for a complete line. 0 returns immediately;
                None waits until a line arrives or the junction stops.

        Returns:
            The data before the delimiter, or None if no complete line is ready.

        Raises:
            ValueError: If `expected` is empty or `max_bytes` is less than 1.
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
            data = bytes(self._buffer[:end])
            del self._buffer[:consumed]
        return data

    def _find_line(self, expected: bytes, max_bytes: int | None) -> tuple[int, int] | None:
        """Return (data length, bytes to consume) for the next line, or None if incomplete."""
        if max_bytes is None:
            idx = self._buffer.find(expected)
        else:
            # Only a delimiter starting within max_bytes ends the line. Cap the chunk once
            # enough bytes are buffered to rule out a delimiter straddling the boundary.
            idx = self._buffer.find(expected, 0, max_bytes + len(expected))
            if idx == -1 and len(self._buffer) >= max_bytes + len(expected):
                return max_bytes, max_bytes
        if idx == -1:
            return None
        return idx, idx + len(expected)

    def readline(self, terminator: bytes = b"\r\n", timeout: float | None = 0) -> bytes | None:
        """Read one line; shorthand for `read_until(terminator, timeout=timeout)`."""
        return self.read_until(terminator, timeout=timeout)

    def write(self, data: str | bytes) -> None:
        """Write to the port, blocking until the data is sent.

        Safe to call from any thread; concurrent writes are serialized.

        Args:
            data: Bytes to send. A str is encoded as UTF-8.

        Raises:
            serial.SerialException: If the port is disconnected or the write
                fails. The data is not retried; the junction reconnects in the
                background.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._write_lock:
            if not self._connected.is_set():
                raise serial.SerialException(f"{self.port} is not connected") from self._error
            ser = self._serial
            try:
                written = ser.write(data)
                if written is not None and written < len(data):
                    raise serial.SerialException(
                        f"Write to {self.port} interrupted after {written} of {len(data)} bytes"
                    )
            except serial.SerialException as e:
                if not self._stop.is_set():
                    logger.error(
                        "Serial write error on %s: %s. Attempting to reconnect", self.port, e
                    )
                self._connected.clear()
                ser.cancel_read()  # Wake the reader thread so it reconnects
                raise
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("SENT %d bytes to %s: %s", len(data), self.port, truncate_data(data))

    def write_latest(self, data: str | bytes) -> None:
        """Queue a value to send in the background, replacing any value still queued.

        Returns immediately. Use it for real-time control, where only the most
        recent command matters: if the junction is busy or disconnected, older
        unsent values are dropped and the latest is sent once the port is ready.

        Args:
            data: Bytes to send. A str is encoded as UTF-8.
        """
        with self._latest_write_lock:
            self._latest_write_data = data
            self._latest_write_event.set()

    def stop(self) -> None:
        """Stop the background threads and close the port. Safe to call more than once."""
        self._stop.set()
        self._connected.clear()
        ser = self._serial
        ser.cancel_read()
        ser.cancel_write()
        with self._buffer_ready:
            self._buffer_ready.notify_all()

        for thread in (self._reader_thread, self._writer_thread):
            if thread is not threading.current_thread():
                thread.join()

        with self._write_lock:
            if self._serial.is_open:
                self._serial.close()
                logger.info("Serial connection closed for %s", self.port)

    def close(self) -> None:
        """Alias for `stop`."""
        self.stop()

    @property
    def running(self) -> bool:
        """False once `stop` has been called or reconnection has given up."""
        return not self._stop.is_set()

    @property
    def is_open(self) -> bool:
        """Whether the port is currently connected."""
        return self._connected.is_set()

    @property
    def in_waiting(self) -> int:
        """Number of bytes in the receive buffer."""
        with self._buffer_ready:
            return len(self._buffer)

    def __enter__(self) -> SerialJunction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
