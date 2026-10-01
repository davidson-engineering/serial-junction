"""Stress tests for ThreadSafeSerial concurrency and buffer handling."""

import threading
import time


# ---------------------------------------------------------------------------
# Concurrent buffer access
# ---------------------------------------------------------------------------

class TestConcurrentBufferAccess:
    """Verify buffer integrity under concurrent read/write from multiple threads."""

    def test_concurrent_reads_no_data_loss(self, serial_manager):
        """Multiple reader threads should collectively consume all buffer data."""
        mgr = serial_manager
        num_messages = 500
        msg = b"ABCDEFGHIJ"  # 10 bytes each

        # Pre-fill buffer
        mgr.input_buffer.extend(msg * num_messages)
        assert len(mgr.input_buffer) == num_messages * len(msg)

        results = []
        lock = threading.Lock()

        def reader():
            while True:
                data = mgr.read(len(msg))
                if not data:
                    break
                with lock:
                    results.append(data)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        total_bytes = sum(len(r) for r in results)
        assert total_bytes == num_messages * len(msg)
        # Reconstruct and verify no corruption
        combined = b"".join(results)
        assert combined == msg * num_messages
        assert len(mgr.input_buffer) == 0

    def test_concurrent_write_and_read(self, serial_manager, feed):
        """One thread extends the buffer while another reads - no crashes or data loss."""
        mgr = serial_manager
        write_count = 1000
        chunk = b"X" * 10
        read_data = []

        def writer():
            for _ in range(write_count):
                feed(mgr, chunk)

        def reader():
            total = 0
            while total < write_count * len(chunk):
                data = mgr.read(len(chunk), timeout=1)
                if not data:
                    return
                read_data.append(data)
                total += len(data)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=5)
        r.join(timeout=5)

        assert sum(len(d) for d in read_data) == write_count * len(chunk)

    def test_concurrent_read_until(self, serial_manager):
        """Multiple threads calling read_until concurrently on a shared buffer."""
        mgr = serial_manager
        num_lines = 200
        line = b"hello\r\n"

        mgr.input_buffer.extend(line * num_lines)

        results = []
        lock = threading.Lock()

        def reader():
            while True:
                data = mgr.read_until(b"\r\n")
                if data is None:
                    break
                with lock:
                    results.append(data)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == num_lines
        assert all(r == b"hello" for r in results)
        assert len(mgr.input_buffer) == 0

    def test_blocking_readers_each_get_whole_lines(self, serial_manager, feed):
        """Readers blocked in readline() are woken by new data and never split a line."""
        mgr = serial_manager
        num_lines = 300
        results = []
        lock = threading.Lock()

        def reader():
            while True:
                line = mgr.readline(timeout=0.5)
                if line is None:
                    return
                with lock:
                    results.append(line)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(num_lines):
            # Split each line across two chunks to exercise partial-line wakeups
            feed(mgr, b"line-%03d" % i)
            feed(mgr, b"\r\n")
        for t in threads:
            t.join(timeout=10)

        assert sorted(results) == [b"line-%03d" % i for i in range(num_lines)]


# ---------------------------------------------------------------------------
# write_latest concurrency
# ---------------------------------------------------------------------------

class TestWriteLatestConcurrency:
    """Verify write_latest is safe under concurrent access."""

    def test_many_concurrent_write_latest(self, serial_manager):
        """Multiple threads hammering write_latest - no crashes, last value wins."""
        mgr = serial_manager
        num_threads = 10
        writes_per_thread = 100
        barrier = threading.Barrier(num_threads)

        def writer(thread_id):
            barrier.wait()
            for i in range(writes_per_thread):
                mgr.write_latest(f"t{thread_id}-{i}".encode())

        threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Each thread's last write is a candidate for the surviving value
        assert mgr._latest_write_event.is_set()
        assert mgr._latest_write_data in {f"t{tid}-{writes_per_thread - 1}".encode() for tid in range(num_threads)}


# ---------------------------------------------------------------------------
# Large buffer operations
# ---------------------------------------------------------------------------

class TestLargeBufferOperations:
    """Test buffer operations with large data volumes."""

    def test_large_buffer_read(self, serial_manager):
        """Reading a large buffer should work correctly."""
        mgr = serial_manager
        large_data = b"X" * 1_000_000  # 1MB
        mgr.input_buffer.extend(large_data)

        result = mgr.read()
        assert len(result) == 1_000_000
        assert result == large_data
        assert len(mgr.input_buffer) == 0

    def test_large_buffer_read_until(self, serial_manager):
        """read_until should handle large payloads between delimiters."""
        mgr = serial_manager
        payload = b"A" * 100_000 + b"\r\n" + b"B" * 50_000
        mgr.input_buffer.extend(payload)

        result = mgr.read_until(b"\r\n")
        assert len(result) == 100_000
        assert result == b"A" * 100_000
        assert bytes(mgr.input_buffer) == b"B" * 50_000

    def test_many_small_read_until(self, serial_manager):
        """Rapidly consuming many small delimited messages."""
        mgr = serial_manager
        num_messages = 10_000
        msg = b"msg\r\n"
        mgr.input_buffer.extend(msg * num_messages)

        count = 0
        while True:
            result = mgr.read_until(b"\r\n")
            if result is None:
                break
            assert result == b"msg"
            count += 1

        assert count == num_messages
        assert len(mgr.input_buffer) == 0

    def test_partial_reads_accumulate_correctly(self, serial_manager):
        """Many small partial reads should collectively return all data."""
        mgr = serial_manager
        total = 50_000
        mgr.input_buffer.extend(b"Z" * total)

        collected = bytearray()
        while len(collected) < total:
            chunk = mgr.read(7)  # odd chunk size
            if not chunk:
                break
            collected.extend(chunk)

        assert len(collected) == total
        assert collected == bytearray(b"Z" * total)


# ---------------------------------------------------------------------------
# Write under load
# ---------------------------------------------------------------------------

class TestWriteUnderLoad:
    """Test blocking write under concurrent pressure."""

    def test_concurrent_blocking_writes(self, serial_manager, mock_serial):
        """Multiple threads calling write() concurrently should all succeed."""
        mgr = serial_manager
        num_threads = 10
        writes_per_thread = 50
        barrier = threading.Barrier(num_threads)
        errors = []

        def writer(thread_id):
            barrier.wait()
            for i in range(writes_per_thread):
                try:
                    mgr.write(f"t{thread_id}-{i}\n".encode())
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0
        assert mock_serial.write.call_count == num_threads * writes_per_thread

    def test_mixed_write_and_write_latest(self, serial_manager, mock_serial):
        """Concurrent write() and write_latest() should not interfere."""
        mgr = serial_manager
        barrier = threading.Barrier(2)
        errors = []

        def blocking_writer():
            barrier.wait()
            for i in range(100):
                try:
                    mgr.write(f"block-{i}\n".encode())
                except Exception as e:
                    errors.append(e)

        def latest_writer():
            barrier.wait()
            for i in range(100):
                mgr.write_latest(f"latest-{i}\n".encode())

        t1 = threading.Thread(target=blocking_writer)
        t2 = threading.Thread(target=latest_writer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0
        # All blocking writes should have gone through
        assert mock_serial.write.call_count == 100

    def test_writes_with_live_writer_thread(self, make_manager, mock_serial):
        """write() and the write_latest thread share the port without errors."""
        mgr = make_manager(writer=True)
        stop = threading.Event()

        def latest_writer():
            i = 0
            while not stop.is_set():
                mgr.write_latest(f"latest-{i}\n".encode())
                i += 1
                time.sleep(0.001)

        t = threading.Thread(target=latest_writer)
        t.start()
        for i in range(200):
            mgr.write(f"block-{i}\n".encode())
        stop.set()
        t.join(timeout=5)

        sent = [c.args[0] for c in mock_serial.write.call_args_list]
        assert [s for s in sent if s.startswith(b"block-")] == [f"block-{i}\n".encode() for i in range(200)]
        assert any(s.startswith(b"latest-") for s in sent)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_read_until_multiple_delimiters(self, serial_manager):
        """Buffer with multiple delimiters - each read_until gets the next one."""
        mgr = serial_manager
        mgr.input_buffer.extend(b"a\r\nb\r\nc\r\n")

        assert mgr.read_until(b"\r\n") == b"a"
        assert mgr.read_until(b"\r\n") == b"b"
        assert mgr.read_until(b"\r\n") == b"c"
        assert mgr.read_until(b"\r\n") is None

    def test_read_until_multi_byte_delimiter(self, serial_manager):
        """Delimiter longer than 2 bytes."""
        mgr = serial_manager
        mgr.input_buffer.extend(b"helloENDworld")

        assert mgr.read_until(b"END") == b"hello"
        assert bytes(mgr.input_buffer) == b"world"

    def test_read_until_delimiter_is_entire_buffer(self, serial_manager):
        """Buffer contains only the delimiter."""
        mgr = serial_manager
        mgr.input_buffer.extend(b"\r\n")

        assert mgr.read_until(b"\r\n") == b""
        assert len(mgr.input_buffer) == 0

    def test_write_empty_bytes(self, serial_manager, mock_serial):
        serial_manager.write(b"")
        mock_serial.write.assert_called_once_with(b"")

    def test_write_empty_string(self, serial_manager, mock_serial):
        serial_manager.write("")
        mock_serial.write.assert_called_once_with(b"")

    def test_repeated_fill_and_drain(self, serial_manager):
        """Filling and draining the buffer repeatedly keeps in_waiting accurate."""
        mgr = serial_manager
        for _ in range(100):
            mgr.input_buffer.extend(b"data")
            assert mgr.in_waiting == 4
            mgr.read()
            assert mgr.in_waiting == 0

        mgr.stop()
        assert mgr.running is False
        assert mgr.in_waiting == 0
