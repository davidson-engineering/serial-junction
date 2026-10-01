"""Read fixed-length binary packets and stream control commands at the same time.

Usage: python examples/binary_packets.py [--port /dev/ttyACM0] [--baudrate 115200]

Expects 10-byte packets framed as 0xA5 <8-byte payload> 0x5A, and sends the
latest setpoint with write_latest(), so a slow link never builds up a backlog
of stale commands. Press Ctrl-C to stop.
"""

import argparse
import logging
import math
import threading
import time

from serial_junction import SerialJunction, WindowedPacketReader


def stream_setpoints(junction: SerialJunction, stop: threading.Event) -> None:
    start = time.monotonic()
    while not stop.wait(0.01):  # 100 Hz control loop
        setpoint = math.sin(time.monotonic() - start)
        junction.write_latest(f"SET {setpoint:+.3f}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial device; auto-detects a USB/ACM device if omitted")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    stop = threading.Event()
    with SerialJunction(args.port, args.baudrate) as junction:
        reader = WindowedPacketReader(junction.read, window_size=10, start_byte=0xA5, end_byte=0x5A)
        sender = threading.Thread(target=stream_setpoints, args=(junction, stop))
        sender.start()
        try:
            while junction.running:
                payload = reader.read_packet()
                if payload is not None:
                    print(f"Packet: {payload.hex(' ')}")
        except KeyboardInterrupt:
            print("Exiting...")
        finally:
            stop.set()
            sender.join()


if __name__ == "__main__":
    main()
