import time
from unittest.mock import patch

import pytest

from serial_junction.packet_reader import PacketReader, WindowedPacketReader


class TestPacketReaderABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            PacketReader(read_callback=lambda: None)

    def test_subclass_must_implement_read_packet(self):
        class IncompleteReader(PacketReader):
            pass

        with pytest.raises(TypeError, match="abstract method"):
            IncompleteReader(read_callback=lambda: None)

    def test_concrete_subclass_works(self):
        class ConcreteReader(PacketReader):
            def read_packet(self):
                return b"data"

        reader = ConcreteReader(read_callback=lambda: None)
        assert reader.read_packet() == b"data"


class TestWindowedPacketReader:
    def test_valid_packet(self):
        """Complete valid packet with start/end bytes."""
        packet_bytes = [bytes([0xA5]), bytes([0x01]), bytes([0x02]), bytes([0x03]), bytes([0x5A])]
        call_iter = iter(packet_bytes)
        reader = WindowedPacketReader(
            read_callback=lambda: next(call_iter, None),
            window_size=5,
            start_byte=0xA5,
            end_byte=0x5A,
            timeout=1.0,
        )
        result = reader.read_packet()
        assert result == bytes([0x01, 0x02, 0x03])

    def test_timeout_no_packet(self):
        reader = WindowedPacketReader(
            read_callback=lambda: None,
            window_size=5,
            timeout=0.05,
        )
        assert reader.read_packet() is None

    def test_incomplete_packet(self):
        data = [bytes([0xA5]), bytes([0x01])]
        call_iter = iter(data)
        reader = WindowedPacketReader(
            read_callback=lambda: next(call_iter, None),
            window_size=5,
            timeout=0.05,
        )
        assert reader.read_packet() is None

    def test_wrong_start_byte(self):
        packet_bytes = [bytes([0xFF]), bytes([0x01]), bytes([0x02]), bytes([0x03]), bytes([0x5A])]
        call_iter = iter(packet_bytes)
        reader = WindowedPacketReader(
            read_callback=lambda: next(call_iter, None),
            window_size=5,
            timeout=0.05,
        )
        assert reader.read_packet() is None

    def test_wrong_end_byte(self):
        packet_bytes = [bytes([0xA5]), bytes([0x01]), bytes([0x02]), bytes([0x03]), bytes([0xFF])]
        call_iter = iter(packet_bytes)
        reader = WindowedPacketReader(
            read_callback=lambda: next(call_iter, None),
            window_size=5,
            timeout=0.05,
        )
        assert reader.read_packet() is None

    def test_noise_then_valid_packet(self):
        """Noise bytes before valid packet - sliding window should align."""
        data = [
            bytes([0xFF]),
            bytes([0xEE]),
            bytes([0xA5]),
            bytes([0x01]),
            bytes([0x02]),
            bytes([0x03]),
            bytes([0x5A]),
        ]
        call_iter = iter(data)
        reader = WindowedPacketReader(
            read_callback=lambda: next(call_iter, None),
            window_size=5,
            timeout=1.0,
        )
        result = reader.read_packet()
        assert result == bytes([0x01, 0x02, 0x03])


PACKET = bytes([0xA5, 0x01, 0x02, 0x03, 0x5A])


def chunks(*parts):
    """A read_callback that returns each part in turn, then nothing."""
    it = iter(parts)
    return lambda: next(it, b"")


class TestWindowedPacketReaderChunking:
    """read_callback returning arbitrary chunks, as SerialJunction.read does."""

    def test_two_packets_in_one_chunk(self):
        reader = WindowedPacketReader(chunks(PACKET + PACKET), window_size=5, timeout=0.05)
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])
        assert reader.read_packet() is None

    def test_packet_followed_by_start_of_next(self):
        second = bytes([0xA5, 0x04, 0x05, 0x06, 0x5A])
        reader = WindowedPacketReader(
            chunks(PACKET + second[:2], second[2:]), window_size=5, timeout=0.05
        )
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])
        assert reader.read_packet() == bytes([0x04, 0x05, 0x06])

    def test_packet_with_trailing_noise_in_same_chunk(self):
        reader = WindowedPacketReader(
            chunks(b"\xff" + PACKET + b"\xee\xdd"), window_size=5, timeout=0.05
        )
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])

    def test_packet_straddling_a_timeout_is_kept(self):
        parts = [PACKET[:3]]
        reader = WindowedPacketReader(
            lambda: parts.pop() if parts else b"", window_size=5, timeout=0.05
        )
        assert reader.read_packet() is None
        parts.append(PACKET[3:])
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])

    def test_noise_stream_still_times_out(self):
        reader = WindowedPacketReader(lambda: b"\x00" * 64, window_size=5, timeout=0.1)
        start = time.monotonic()
        assert reader.read_packet() is None
        assert time.monotonic() - start < 2

    def test_zero_timeout_polls_once(self):
        reader = WindowedPacketReader(chunks(PACKET), window_size=5, timeout=0)
        assert reader.read_packet() == bytes([0x01, 0x02, 0x03])

    def test_sleeps_between_empty_polls(self):
        reader = WindowedPacketReader(lambda: None, window_size=5, timeout=0.05, poll_interval=0.01)
        with patch("serial_junction.packet_reader.time.sleep", wraps=time.sleep) as sleep:
            assert reader.read_packet() is None
        assert sleep.call_count >= 1
        assert all(c.args == (0.01,) for c in sleep.call_args_list)


def test_window_must_hold_start_and_end_bytes():
    with pytest.raises(ValueError, match="window_size"):
        WindowedPacketReader(lambda: None, window_size=1)
