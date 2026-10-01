# serial-junction

[![PyPI](https://img.shields.io/pypi/v/serial-junction)](https://pypi.org/project/serial-junction/)
[![Python versions](https://img.shields.io/pypi/pyversions/serial-junction)](https://pypi.org/project/serial-junction/)
[![CI](https://github.com/davidson-engineering/serial-junction/actions/workflows/ci.yml/badge.svg)](https://github.com/davidson-engineering/serial-junction/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/pypi/l/serial-junction)](https://github.com/davidson-engineering/serial-junction/blob/main/LICENSE)

**One serial port, many threads.** `serial-junction` lets any number of threads
in your program read from and write to the same serial port safely. It buffers
incoming bytes in the background and reconnects automatically when the device
drops. It is a software library built on [pyserial](https://github.com/pyserial/pyserial),
not a hardware splitter.

```python
from serial_junction import SerialJunction

with SerialJunction("/dev/ttyUSB0", 115200) as junction:
    junction.write(b"*IDN?\r\n")
    print(junction.readline(timeout=1.0))
```

## Why serial-junction?

pyserial gives you a port, and sharing it between threads is left to you. Two
threads reading from one `serial.Serial` split the incoming bytes between them
unpredictably, concurrent writes can interleave, and when a USB adapter is
unplugged the port stays dead until you rebuild it yourself.

`serial-junction` puts a single background thread in charge of the port:

- **Every byte lands in one buffer.** Threads take whole lines, chunks or framed
  packets from it, waiting up to a timeout you choose.
- **Any thread can write.** Writes are serialized, and raise
  `serial.SerialException` when the device is gone instead of failing silently.
- **`write_latest()` for control loops.** Queue a command without blocking; if
  the link is busy or down, only the newest command is sent.
- **Automatic reconnection.** When the device drops, the junction reopens it and
  your threads carry on.
- **Packet framing** for fixed-length binary protocols.
- **Typed and tested** end to end against real pseudo-terminals, including on
  free-threaded Python.

## Installation

```bash
pip install serial-junction
```

Or with [uv](https://docs.astral.sh/uv/): `uv add serial-junction`.
Requires Python 3.10 or newer.

## Usage

### Opening a port

```python
from serial_junction import SerialJunction

junction = SerialJunction("/dev/ttyUSB0", 115200)  # or "COM3" on Windows
junction = SerialJunction(baudrate=115200)  # first device matching "ACM|USB"
junction = SerialJunction(search_pattern="CP210", baudrate=115200)
```

The constructor blocks until the port opens. By default it keeps retrying until
a device appears; pass `max_reconnect_attempts=3` to fail with
`serial.SerialException` instead. Use it as a context manager, or call
`junction.stop()` when you are done.

### Reading

Received bytes collect in a buffer. Each read takes bytes out of it, so with
several reader threads every byte goes to exactly one of them.

```python
line = junction.readline(timeout=1.0)  # b"OK" (terminator removed), or None after 1 s
line = junction.readline(b"\n")  # custom terminator; returns at once
chunk = junction.read(64, timeout=0.5)  # up to 64 bytes
everything = junction.read()  # whatever is buffered, possibly b""
```

| `timeout` | Behaviour |
|---|---|
| `0` (default) | Return immediately with what is buffered |
| seconds | Wait up to that long |
| `None` | Wait until data arrives or the junction stops |

To guard against a device that never sends a terminator, pass `max_bytes`:
`junction.read_until(b"\n", max_bytes=256)` returns a runaway line in 256-byte
pieces.

### Writing

```python
junction.write(b"MEASURE\r\n")  # blocks until sent; a str is encoded as UTF-8
```

`write()` raises `serial.SerialException` if the device is disconnected or the
write fails. The data is not retried, so you decide whether to resend it.

For control loops, `write_latest()` queues a value and returns immediately. A
background thread sends it as soon as the port is free. If you queue a new value
before the old one is sent, the old one is dropped, so a slow or reconnecting
link never builds up a backlog of stale commands:

```python
while running:
    junction.write_latest(f"SET {controller.output():.3f}\n")
    time.sleep(0.01)
```

### Reconnection

When a read or write fails, the background thread closes the port and reopens
it every `reconnect_delay` seconds (default 1):

- An explicit `port` is always reopened at the same path. It is never swapped
  for another device, even if one matches `search_pattern`.
- With `port=None`, the device you were last connected to is preferred.
- Data already received stays in the buffer.
- While disconnected, `is_open` is `False` and `write()` raises.
- After `max_reconnect_attempts` failed attempts (0, the default, means retry
  forever), the junction gives up: `running` becomes `False`, blocked readers
  return, and `write()` raises with the last connection error as its cause.

### Binary packets

`WindowedPacketReader` extracts fixed-length packets framed by a start byte and
an end byte, skipping any noise in between:

```python
from serial_junction import SerialJunction, WindowedPacketReader

junction = SerialJunction("/dev/ttyACM0", 115200)
reader = WindowedPacketReader(junction.read, window_size=10, start_byte=0xA5, end_byte=0x5A)

payload = reader.read_packet()  # the 8 bytes between 0xA5 and 0x5A, or None after 1 s
```

Packets that arrive together, or are split across reads, are handled; bytes
after a packet are kept for the next call.

### Stopping

`stop()` (or `close()`, or leaving the `with` block) stops both background
threads, releases any thread blocked in a read or write, and closes the port. It
is safe to call more than once.

### Logging

Connection events are logged to the `serial_junction` logger at INFO and ERROR
level. Enable DEBUG to log every chunk sent and received:

```python
logging.getLogger("serial_junction").setLevel(logging.DEBUG)
```

## API reference

### `SerialJunction(port=None, baudrate=9600, *, ...)`

| Parameter | Default | Description |
|---|---|---|
| `port` | `None` | Device path such as `"/dev/ttyUSB0"` or `"COM3"`; `None` auto-detects |
| `baudrate` | `9600` | Baud rate |
| `bytesize`, `parity`, `stopbits` | 8N1 | As for `serial.Serial` |
| `max_reconnect_attempts` | `0` | Failed attempts before giving up; `0` retries forever |
| `reconnect_delay` | `1.0` | Seconds between connection attempts |
| `search_pattern` | `r"ACM\|USB"` | Regex matched against device paths and descriptions when auto-detecting |
| `timeout` | `1` | Read timeout of the background reader thread; rarely needs changing |

| Method or property | Description |
|---|---|
| `read(size=-1, timeout=0)` | Up to `size` bytes from the buffer (`-1` for all) |
| `read_until(expected=b"\r\n", max_bytes=None, timeout=0)` | Data up to a delimiter, or `None` |
| `readline(terminator=b"\r\n", timeout=0)` | Shorthand for `read_until` |
| `write(data)` | Blocking write; raises `serial.SerialException` on failure |
| `write_latest(data)` | Non-blocking write that keeps only the newest value |
| `detect_devices()` | Device paths matching `search_pattern` |
| `stop()` / `close()` | Stop the background threads and close the port |
| `in_waiting` | Number of buffered bytes |
| `is_open` | Whether the port is currently connected |
| `running` | `False` once stopped or once reconnection has given up |
| `port` | The device currently in use |

### `WindowedPacketReader(read_callback, window_size=10, start_byte=0xA5, end_byte=0x5A, timeout=1.0, poll_interval=0.001)`

| Parameter | Description |
|---|---|
| `read_callback` | Called with no arguments to fetch bytes, e.g. `junction.read` |
| `window_size` | Total packet length, including the start and end bytes |
| `start_byte`, `end_byte` | Framing byte values |
| `timeout` | Seconds `read_packet()` waits for a packet |
| `poll_interval` | Seconds to sleep when `read_callback` returns nothing |

`read_packet()` returns the payload between the framing bytes, or `None` on timeout.

## Examples

- [`line_protocol.py`](https://github.com/davidson-engineering/serial-junction/blob/main/examples/line_protocol.py):
  one thread sends lines while another prints the replies.
- [`binary_packets.py`](https://github.com/davidson-engineering/serial-junction/blob/main/examples/binary_packets.py):
  reads framed binary packets while streaming setpoints with `write_latest()`.

## Platform notes

- **Linux:** your user needs permission to open serial devices, usually by
  joining the `dialout` group (`uucp` on Arch): `sudo usermod -aG dialout $USER`,
  then log in again.
- **macOS:** open USB adapters through their `/dev/cu.*` path, for example
  `/dev/cu.usbserial-1410`.
- **Windows:** use `COM` port names such as `"COM3"`.

## Migrating from threadsafe-serial

This package was previously `python-threadsafe-serial`. The old imports still
work but emit a `DeprecationWarning`:

| Before | After |
|---|---|
| `from threadsafe_serial import ThreadSafeSerial` | `from serial_junction import SerialJunction` |
| `threadsafe_serial.threadsafe_serial` | `serial_junction.junction` |
| `threadsafe_serial.packet_reader` | `serial_junction.packet_reader` |

Behaviour changes in this release:

- `write()` raises `serial.SerialException` on failure instead of dropping the data silently.
- Arguments after `baudrate` are keyword-only.
- The wait between reconnection attempts is now `reconnect_delay`, no longer `timeout`.
- `detect_devices()` returns an empty list instead of `None` when nothing matches.
- `read_until(max_bytes=...)` now caps the returned data at `max_bytes`.
- Log records come from the `serial_junction.junction` logger.

## Development

```bash
uv sync
uv run pytest                 # tests; the pseudo-terminal tests are skipped on Windows
uv run ruff check && uv run ruff format --check
uv run mypy
```

## License

MIT. See [LICENSE](https://github.com/davidson-engineering/serial-junction/blob/main/LICENSE).
