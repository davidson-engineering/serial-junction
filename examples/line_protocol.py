"""Share one serial port between a sender thread and a receiver thread.

Usage: python examples/line_protocol.py [--port /dev/ttyUSB0] [--baudrate 115200]

Sends a line every 100 ms and prints every line the device sends back.
Press Ctrl-C to stop.
"""

import argparse
import logging
import random
import threading
import time

import serial

from serial_junction import SerialJunction


def send_lines(junction: SerialJunction, stop: threading.Event) -> None:
    while not stop.wait(0.1):
        try:
            junction.write(random.choice(["Hello\n", "World\n", "123\n"]))
        except serial.SerialException as e:
            print(f"Write failed: {e}")  # the junction reconnects in the background


def print_lines(junction: SerialJunction) -> None:
    while junction.running:
        line = junction.readline(b"\n", timeout=1.0)
        if line is not None:
            print(f"Received: {line!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial device; auto-detects a USB/ACM device if omitted")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    stop = threading.Event()
    with SerialJunction(args.port, args.baudrate) as junction:
        threads = [
            threading.Thread(target=send_lines, args=(junction, stop)),
            threading.Thread(target=print_lines, args=(junction,)),
        ]
        for thread in threads:
            thread.start()
        try:
            while junction.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("Exiting...")
        finally:
            stop.set()

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
