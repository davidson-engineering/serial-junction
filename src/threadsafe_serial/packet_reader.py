from abc import ABC, abstractmethod
from collections import deque
import time
from typing import Callable, Optional


class PacketReader(ABC):
    """Abstract base class for packet readers."""

    def __init__(self, read_callback: Callable[[], Optional[bytes]]) -> None:
        self.read_callback = read_callback

    @abstractmethod
    def read_packet(self) -> Optional[bytes]:
        pass


class WindowedPacketReader(PacketReader):
    """Sliding window packet reader with start/end byte framing.

    `read_callback` may return any number of bytes per call. Bytes are kept
    across calls, so packets that arrive together, or straddle a timeout, are
    not lost. When the callback returns nothing, the reader sleeps
    `poll_interval` seconds before polling again.
    """

    def __init__(
        self,
        read_callback: Callable[[], Optional[bytes]],
        window_size: int = 10,
        start_byte: int = 0xA5,
        end_byte: int = 0x5A,
        timeout: float = 1.0,
        poll_interval: float = 0.001,
    ) -> None:
        super().__init__(read_callback)
        self.window_size = window_size
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._window: deque[int] = deque(maxlen=window_size)
        self._pending = bytearray()

    def read_packet(self) -> Optional[bytes]:
        """Return the next packet's payload, or None if none arrives within timeout."""
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

    def _scan(self) -> Optional[bytes]:
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
