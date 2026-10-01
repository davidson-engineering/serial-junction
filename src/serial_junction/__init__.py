"""One serial port shared safely between threads.

    >>> from serial_junction import SerialJunction
    >>> with SerialJunction("/dev/ttyUSB0", 115200) as junction:  # doctest: +SKIP
    ...     junction.write(b"PING\\r\\n")
    ...     reply = junction.readline(timeout=1.0)

See https://github.com/davidson-engineering/serial-junction for documentation.
"""

from importlib.metadata import PackageNotFoundError, version

from .junction import SerialJunction
from .packet_reader import PacketReader, WindowedPacketReader

try:
    __version__ = version("serial-junction")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["PacketReader", "SerialJunction", "WindowedPacketReader", "__version__"]
