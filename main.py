#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Created By  : Matthew Davidson
# Created Date: 2024-01-01
# version ='0.0.1'
# ---------------------------------------------------------------------------
"""Demo: one serial port shared by a sender thread and a receiver thread."""
# ---------------------------------------------------------------------------

import logging
import random
import threading
import time

import serial

from serial_junction import SerialJunction


def send_data(junction: SerialJunction, stop: threading.Event):
    """Send a message to the serial port every 100 ms."""
    while not stop.wait(0.1):
        try:
            junction.write(random.choice(["Hello\n", "World\n", "123\n"]))
        except serial.SerialException as e:
            print(f"Write failed: {e}")


def listen_for_data(junction: SerialJunction):
    """Print each line received from the serial port."""
    while junction.running:
        data = junction.readline(b"\n", timeout=1.0)
        if data is not None:
            print(f"Received data: {data}")


def main():
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()

    # Create a shared instance of SerialJunction
    with SerialJunction(baudrate=115200, search_pattern=r"ACM|USB") as junction:
        threads = [
            threading.Thread(target=send_data, args=(junction, stop)),
            threading.Thread(target=listen_for_data, args=(junction,)),
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
