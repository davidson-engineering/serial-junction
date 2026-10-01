from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("serial-junction")
except PackageNotFoundError:
    __version__ = "0.0.0"

from .junction import SerialJunction
from .packet_reader import PacketReader, WindowedPacketReader

__all__ = ["SerialJunction", "PacketReader", "WindowedPacketReader", "__version__"]
