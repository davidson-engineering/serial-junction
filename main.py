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

from threadsafe_serial import ThreadSafeSerial


def send_data(serial_manager: ThreadSafeSerial, stop: threading.Event):
    """Send a message to the serial port every 100 ms."""
    while not stop.wait(0.1):
        try:
            serial_manager.write(random.choice(["Hello\n", "World\n", "123\n"]))
        except serial.SerialException as e:
            print(f"Write failed: {e}")


def listen_for_data(serial_manager: ThreadSafeSerial):
    """Print each line received from the serial port."""
    while serial_manager.running:
        data = serial_manager.readline(b"\n", timeout=1.0)
        if data is not None:
            print(f"Received data: {data}")


def main():
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()

    # Create a shared instance of ThreadSafeSerial
    with ThreadSafeSerial(baudrate=115200, search_pattern=r"ACM|USB") as serial_manager:
        threads = [
            threading.Thread(target=send_data, args=(serial_manager, stop)),
            threading.Thread(target=listen_for_data, args=(serial_manager,)),
        ]
        for thread in threads:
            thread.start()

        try:
            while serial_manager.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("Exiting...")
        finally:
            stop.set()

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
