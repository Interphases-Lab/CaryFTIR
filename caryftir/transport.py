
"""
USB access and the framing layer.

"""
import logging
import os
import struct
from dataclasses import dataclass

from .protocol import (BLOCKED_COMMANDS, DATA_CHUNK_SIZE, EP_COMMAND_OUT, EP_DATA_IN, EP_DATA_OUT,
                       EP_REPLY_IN, MAX_INLINE_MESSAGE, PACKET_SIZE, Frame, FrameType, ProtocolError,
                       decode_frame, encode_frame)

log = logging.getLogger(__name__)

VENDOR_ID = 0x0FB4
PRODUCT_ID = 0x0201


class BlockedCommandError(ProtocolError):
    pass


@dataclass(frozen=True)
class UsbLocation:
    """Where an instrument is connected. `address` changes when it is re-plugged; `port_path` does not."""
    bus: int
    address: int
    port_path: str  # e.g. "1-2.3": bus, then the hub ports from the root

    def __str__(self) -> str:
        return f"bus {self.bus}, address {self.address} (port {self.port_path})"


def _location(dev) -> UsbLocation:
    ports = ".".join(str(p) for p in (dev.port_numbers or ()))
    return UsbLocation(dev.bus, dev.address, f"{dev.bus}-{ports}" if ports else str(dev.bus))


def _find_devices(vendor_id: int, product_id: int) -> list:
    import usb.core
    return list(usb.core.find(find_all=True, idVendor=vendor_id, idProduct=product_id))


def find_instruments(vendor_id: int = VENDOR_ID, product_id: int = PRODUCT_ID) -> list:
    """Locations of all connected instruments."""
    return [_location(d) for d in _find_devices(vendor_id, product_id)]


def select_device(devices: list, bus: int = None, address: int = None, port_path: str = None):
    """The device matching all given criteria, or the only device if none is given."""
    criteria = {k: v for k, v in (("bus", bus), ("address", address), ("port_path", port_path)) if v is not None}
    if criteria:
        matches = [d for d in devices if all(getattr(_location(d), k) == v for k, v in criteria.items())]
        wanted = ", ".join(f"{k} {v}" for k, v in criteria.items())
        if not matches:
            raise IOError(f"no instrument at {wanted}; connected: "
                          f"{'; '.join(str(_location(d)) for d in devices) or 'none'}")
        if len(matches) > 1:
            raise IOError(f"several instruments match {wanted}; add bus and address, or use port_path")
        return matches[0]
    if not devices:
        raise IOError("no instrument connected")
    if len(devices) > 1:
        raise IOError("several instruments connected; choose one with port_path= or bus=/address=: "
                      + "; ".join(str(_location(d)) for d in devices))
    return devices[0]


class UsbEndpoints:
    """Raw bulk endpoints of the instrument (interface 0), via pyusb/libusb."""

    def __init__(self, bus: int = None, address: int = None, port_path: str = None, vendor_id: int = VENDOR_ID,
                 product_id: int = PRODUCT_ID, timeout_ms: int = 5000):
        """Open the instrument matching `bus`/`address`/`port_path`, or the only connected one."""
        import usb.util
        self._usb = usb
        self.timeout_ms = timeout_ms
        self.dev = select_device(_find_devices(vendor_id, product_id), bus, address, port_path)
        self.location = _location(self.dev)
        if os.name == "posix" and self.dev.is_kernel_driver_active(0):
            self.dev.detach_kernel_driver(0)
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        log.info("opened instrument at %s", self.location)

    def write(self, endpoint: int, data: bytes) -> None:
        written = self.dev.write(endpoint, data, timeout=self.timeout_ms)
        if written != len(data):
            raise IOError(f"short write on EP 0x{endpoint:02x}: {written}/{len(data)}")

    def read(self, endpoint: int, size: int, timeout_ms: int = None) -> bytes:
        return bytes(self.dev.read(endpoint, size, timeout=timeout_ms or self.timeout_ms))

    def close(self) -> None:
        self._usb.util.release_interface(self.dev, 0)
        self._usb.util.dispose_resources(self.dev)
        log.debug("USB device released")


class Link:
    """Framing: sequence numbers, command/reply pairs and the two data pipes."""

    def __init__(self, endpoints):
        self.ep = endpoints
        self._seq = 0

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    def _send(self, frame_type: int, seq: int, body: bytes) -> None:
        data = encode_frame(frame_type, seq, body)
        log.debug("OUT %s", data[:4 + len(body)].hex(" "))
        self.ep.write(EP_COMMAND_OUT, data)

    def _receive(self, seq: int, expect_type: int = None) -> Frame:
        frame = decode_frame(self.ep.read(EP_REPLY_IN, PACKET_SIZE))
        log.debug("IN  %s", frame)
        if frame.type == FrameType.REJECT:
            raise ProtocolError(f"device rejected frame seq=0x{seq:02x}: {frame}")
        if frame.seq != seq:
            raise ProtocolError(f"reply sequence 0x{frame.seq:02x} != request 0x{seq:02x}")
        if expect_type is not None and frame.type != expect_type:
            raise ProtocolError(f"expected frame type 0x{expect_type:02x}, got {frame}")
        return frame

    def reset(self) -> Frame:
        """Link reset; must be the first frame of a session."""
        self._seq = 0
        seq = self._next_seq()
        self._send(FrameType.RESET, seq, b"")
        return self._receive(seq, FrameType.RESET)

    def request(self, body: bytes) -> Frame:
        """Send one application message and return the device's reply frame."""
        if body and body[0] in BLOCKED_COMMANDS:
            raise BlockedCommandError(f"command 0x{body[0]:02x} is blocked for safety (protocol.BLOCKED_COMMANDS)")
        seq = self._next_seq()
        if len(body) <= MAX_INLINE_MESSAGE:
            self._send(FrameType.COMMAND, seq, body)
        else:
            # Announce the length, wait for the echo, then stream the message on EP 0x06.
            self._send(FrameType.OUT_DATA, seq, struct.pack(">I", len(body)))
            self._receive(seq, FrameType.OUT_DATA)
            log.debug("OUT6 %s", body.hex(" "))
            for i in range(0, len(body), DATA_CHUNK_SIZE):
                self.ep.write(EP_DATA_OUT, body[i:i + DATA_CHUNK_SIZE])
        return self._receive(seq)

    def read_data(self, length: int) -> bytes:
        """Read exactly `length` bytes announced by a FrameType.IN_DATA reply."""
        data = bytearray()
        while len(data) < length:
            want = -(-(length - len(data)) // DATA_CHUNK_SIZE) * DATA_CHUNK_SIZE
            chunk = self.ep.read(EP_DATA_IN, want)
            if not chunk:
                raise ProtocolError(f"data pipe returned nothing after {len(data)}/{length} bytes")
            data += chunk
        if len(data) != length:
            raise ProtocolError(f"data pipe returned {len(data)} bytes, expected {length}")
        return bytes(data)
