"""Deprecated alias for serial_junction, kept so existing imports keep working."""
import warnings

from serial_junction import PacketReader, SerialJunction, WindowedPacketReader, __version__

warnings.warn(
    "threadsafe_serial has been renamed to serial_junction, and ThreadSafeSerial to SerialJunction. "
    "Update your imports; this alias will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

ThreadSafeSerial = SerialJunction

__all__ = ["ThreadSafeSerial", "PacketReader", "WindowedPacketReader", "__version__"]
