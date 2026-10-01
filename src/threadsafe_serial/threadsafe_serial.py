"""Deprecated alias for serial_junction.junction."""

from serial_junction.junction import SerialJunction, truncate_data

ThreadSafeSerial = SerialJunction

__all__ = ["ThreadSafeSerial", "truncate_data"]
