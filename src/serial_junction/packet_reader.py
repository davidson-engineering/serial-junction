"""Framing helpers that turn a byte stream into packets."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable


class PacketReader(ABC):
    """Base class for readers that pull packets from a byte source.

    Args:
        read_callback: Called with no arguments to fetch more bytes. It may
            return any number of bytes, or b"" / None when nothing is available.
    """

    def __init__(self, read_callback: Callable[[], bytes | None]) -> None:
        self.read_callback = read_callback

    @abstractmethod
    def read_packet(self) -> bytes | None:
        """Return the next packet, or None if none is available."""


class WindowedPacketReader(PacketReader):
    """Fixed-length packets framed by a start byte and an end byte.

    Slides a `window_size`-byte window over the incoming stream and returns the
    payload between the start and end bytes when both line up. Noise before a
    packet is skipped. Bytes are kept across calls, so packets that arrive
    together, or straddle a timeout, are not lost.

    Args:
        read_callback: Called with no arguments to fetch more bytes, e.g.
            `junction.read`. It may return any number of bytes.
        window_size: Total packet length, including the start and end bytes.
        start_byte: Value of the first byte of a packet.
        end_byte: Value of the last byte of a packet.
        timeout: Seconds `read_packet` waits for a packet.
        poll_interval: Seconds to sleep when `read_callback` returns nothing.

    Raises:
        ValueError: If `window_size` is less than 2.
    """

    def __init__(
        self,
        read_callback: Callable[[], bytes | None],
        window_size: int = 10,
        start_byte: int = 0xA5,
        end_byte: int = 0x5A,
        timeout: float = 1.0,
        poll_interval: float = 0.001,
    ) -> None:
        if window_size < 2:
            raise ValueError("window_size must be at least 2 (start and end bytes)")
        super().__init__(read_callback)
        self.window_size = window_size
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._window: deque[int] = deque(maxlen=window_size)
        self._pending = bytearray()

    def read_packet(self) -> bytes | None:
        """Return the next packet's payload, or None if none arrives within `timeout`."""
        deadline = time.monotonic() + self.timeout
        packet = self._scan()
        while packet is None:
            data = self.read_callback()
            if data:
                self._pending.extend(data)
                packet = self._scan()
            elif time.monotonic() < deadline:
                time.sleep(self.poll_interval)
            if packet is None and time.monotonic() >= deadline:
                break
        return packet

    def _scan(self) -> bytes | None:
        """Slide the window over pending bytes and return the first framed payload."""
        for i, byte in enumerate(self._pending):
            self._window.append(byte)
            if (
                len(self._window) == self.window_size
                and self._window[0] == self.start_byte
                and self._window[-1] == self.end_byte
            ):
                del self._pending[: i + 1]
                packet = bytes(self._window)[1:-1]
                self._window.clear()
                return packet
        self._pending.clear()
        return None
