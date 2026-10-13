
"""
Wire format of the Cary 630 USB protocol: constants and message encoding. No I/O.

This is a best-effort description based on USB traffic; see docs/protocol.md.

Every packet on the command pipes is 64 bytes::

    [type][seq][len_hi][len_lo] [message, `len` bytes] [zero padding]

and a message (command or reply) starts with::

    [command][address][status][parameter byte count] [big-endian fields ...]
"""
import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

PACKET_SIZE = 64
MAX_INLINE_MESSAGE = PACKET_SIZE - 4  # longer messages go via FrameType.OUT_DATA + EP 0x06
DATA_CHUNK_SIZE = 512                 # USB packet size of the data pipes

EP_COMMAND_OUT = 0x04
EP_REPLY_IN = 0x83
EP_DATA_OUT = 0x06
EP_DATA_IN = 0x85


class FrameType(IntEnum):
    COMMAND = 0x08
    REPLY = 0x09
    OUT_DATA = 0x0A   # host announces a message on EP 0x06; body = BE32 length
    IN_DATA = 0x0B    # device announces a message on EP 0x85; body = BE32 length
    REJECT = 0x0C     # device refused the frame
    RESET = 0x0D      # first frame of a session


class Status(IntEnum):
    OK = 0x40         # '@'
    ERROR = 0x41      # 'A'
    SCANNING = 0x43   # 'C'  POLL_SCAN: no scan ready yet
    DONE = 0x44       # 'D'  last chunk of a block on EP 0x85
    FAILED = 0x45     # 'E'
    BUSY = 0x60       # '`'  transaction still running


class Cmd(IntEnum):
    CONNECT = 0x01
    DISCONNECT = 0x02
    CONTROL = 0x15            # carries a Control message id
    READ_VALUE_LIST = 0x18
    CONNECT_CONFIRM = 0x19
    POLL_SCAN = 0x60
    RELEASE_SCAN = 0x61
    SELECT_BLOCK = 0x62       # select one block of the current scan
    RELEASE_BLOCK = 0x63
    READ_BLOCK = 0x64         # read the selected block in chunks over EP 0x85
    GET_DATA_TYPES = 0x68     # data types the instrument can deliver
    REQUEST_DATA_TYPES = 0x69  # data types the host wants per scan
    GET_VALUE = 0xB2
    SET_VALUE = 0xB3
    SET_VALUES = 0xB4
    POLL_TRANSACTION = 0xB5
    REFRESH_INFO = 0xB6
    WRITE_NONVOLATILE = 0xB1  # blocked, see BLOCKED_COMMANDS
    # Known but not used by this driver: 0xB0, 0xB7, 0xA4, 0xB8, 0xB9.


# Commands the driver refuses to send, for safety. WRITE_NONVOLATILE changes values the
# instrument keeps across power cycles (e.g. calibration). Normal operation never needs it;
# unblock only deliberately, for a specific service task.
BLOCKED_COMMANDS = frozenset({Cmd.WRITE_NONVOLATILE})


class Control(IntEnum):
    """Message ids for Cmd.CONTROL."""
    START_SCANNING = 0x10
    STOP_SCANNING = 0x11
    START_BACKGROUND = 0x20
    START_SAMPLE = 0x21


class DataType(IntFlag):
    """Block types; also used as bits in the data-type masks."""
    RAW = 0x0001               # interleaved IR + laser records
    RAW_IR = 0x0002
    RAW_LASER = 0x0004
    INTERFEROGRAM = 0x0010     # resampled interferogram
    VELOCITY = 0x0040
    SINGLE_BEAM = 0x0100
    MINI_SINGLE_BEAM = 0x0200
    SPECTRUM = 0x0400
    SCAN_INFO = 0x4000         # value list describing the scan


FLOAT_TYPES = DataType.VELOCITY | DataType.SINGLE_BEAM | DataType.MINI_SINGLE_BEAM | DataType.SPECTRUM | 0x0800


class ValueList(IntEnum):
    """Lists for Cmd.READ_VALUE_LIST, read once after connecting."""
    SCAN_INFO = 0x4000
    SYSTEM_INFO = 0x2000


class Tag(IntEnum):
    """Instrument values. The low nibble seems to encode the type: 4 float, e string, else integer."""
    RUN_ID = 0x0031
    RUN_MODE = 0x0041
    RESOLUTION = 0x0101              # cm-1
    PHASE_POINTS = 0x0111
    LEFT_PAD = 0x0121
    RIGHT_PAD = 0x0131
    AVERAGE_COUNT = 0x0141           # scans co-added per averaged result
    UNKNOWN_0221 = 0x0221
    INTERFEROGRAM_PEAK = 0x0231
    UNKNOWN_0241 = 0x0241
    SERIAL_NUMBER = 0x102E
    SCAN_MODE = 0x1041               # 4 = normal
    IR_GAIN = 0x1201
    LASER_GAIN = 0x1211
    # Status values in every scan's info; names as the original software's status display uses them.
    SHUTDOWN_FLAGS = 0x1901          # bit 0x01: instrument shutting down, bit 0x10: battery critical
    SAMPLING_ACCESSORY = 0x1911      # changes when the sampling accessory changes
    CPU_TEMPERATURE = 0x1944         # deg C
    POWER_TEMPERATURE = 0x1954       # deg C
    IR_TEMPERATURE = 0x1964          # deg C
    DETECTOR_TEMPERATURE = 0x1974    # deg C
    BLOCK_TEMPERATURE = 0x1984       # deg C; also used for the laser temperature correction
    SOURCE_VOLTAGE = 0x19E4
    SOURCE_CURRENT = 0x19F4
    APODIZATION = 0x3101
    ZERO_FILL = 0x3111
    OFFSET_CORRECTION = 0x3121
    PHASE_CORRECTION = 0x3131
    PHASE_APODIZATION = 0x3141
    MAX_WAVENUMBER = 0x3154
    LASER_WAVENUMBER = 0x3184        # educated guess: laser wavenumber at 0 deg C (calibration offset)
    LASER_TEMPERATURE_COEFFICIENT = 0x3194  # educated guess: cm-1 per deg C (calibration slope)
    AXIS_CORRECTION_OFF = 0x31A1     # educated guess: 1 disables temperature correction in the original software
    AXIS_CORRECTION_APPLIED = 0x31C1  # educated guess: which correction the original software applied
    FIRST_WAVENUMBER = 0x3904        # x axis of SINGLE_BEAM / SPECTRUM blocks (guess)
    LAST_WAVENUMBER = 0x3914


class ProtocolError(IOError):
    pass


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    type: int
    seq: int
    body: bytes

    @property
    def cmd(self) -> int:
        return self.body[0]

    @property
    def status(self) -> int:
        return self.body[2]

    def u32(self, offset: int) -> int:
        return struct.unpack_from(">I", self.body, offset)[0]

    def u16(self, offset: int) -> int:
        return struct.unpack_from(">H", self.body, offset)[0]

    def __str__(self) -> str:
        return f"Frame(type=0x{self.type:02x}, seq=0x{self.seq:02x}, body={self.body.hex(' ')})"


def encode_frame(frame_type: int, seq: int, body: bytes = b"") -> bytes:
    if len(body) > MAX_INLINE_MESSAGE:
        raise ValueError(f"{len(body)}-byte message does not fit in one packet")
    return struct.pack(">BBH", frame_type, seq & 0xFF, len(body)) + body.ljust(MAX_INLINE_MESSAGE, b"\0")


def decode_frame(raw: bytes) -> Frame:
    if len(raw) < 4:
        raise ProtocolError(f"short frame: {bytes(raw).hex()}")
    frame_type, seq, length = struct.unpack_from(">BBH", raw)
    return Frame(frame_type, seq, bytes(raw[4:4 + length]))


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def message(cmd: int, *fields: int, addr: int = 0, n_param_bytes: int = 0,
            length: int = 16, tail: bytes = b"") -> bytes:
    """[cmd][addr][0][n] + BE32 fields, zero-padded to `length`, then `tail`."""
    head = bytes([cmd, addr & 0xFF, 0, n_param_bytes]) + b"".join(struct.pack(">I", f & 0xFFFFFFFF) for f in fields)
    return head.ljust(length, b"\0") + tail


def connect_msg() -> bytes:
    return message(Cmd.CONNECT, 0x012A0000)  # constant 01 2a, as observed


def request_data_types_msg(single: int, averaged: int) -> bytes:
    return message(Cmd.REQUEST_DATA_TYPES, single, averaged)


def read_value_list_msg(value_list: int, addr: int = 0) -> bytes:
    return message(Cmd.READ_VALUE_LIST, value_list, addr=addr)


def get_value_msg(tag: int) -> bytes:
    return message(Cmd.GET_VALUE, tag, length=48)


def set_value_msg(tag: int, value: bytes, flag: int) -> bytes:
    body = message(Cmd.SET_VALUE, tag, len(value) << 16, flag)
    return (body + value).ljust(48, b"\0")


def set_values_msg(tlvs: bytes, transaction: int, device: int = 0) -> bytes:
    return message(Cmd.SET_VALUES, len(tlvs) << 16, (device << 24) | ((transaction & 0xFF) << 16), tail=tlvs)


def transaction_msg(cmd: int, transaction: int, device: int = 0) -> bytes:
    """REFRESH_INFO / POLL_TRANSACTION: [cmd 0 0 0][0][device, transaction, 0, 0]."""
    return message(cmd, 0, (device << 24) | ((transaction & 0xFF) << 16))


def control_msg(control: int, data: bytes) -> bytes:
    # Bytes 10-11 are 0x0004 in every observed message.
    body = message(Cmd.CONTROL, (control << 24) | (1 << 16), (len(data) << 16) | 0x0004,
                   n_param_bytes=8, tail=data)
    return body.ljust(len(data) + 32, b"\0")


def select_block_msg(data_type: int, averaged: bool = False) -> bytes:
    return message(Cmd.SELECT_BLOCK, data_type, int(averaged))


def read_block_msg(scan_index: int) -> bytes:
    return message(Cmd.READ_BLOCK, addr=scan_index)


# --------------------------------------------------------------------------- #
# Values and value lists
# --------------------------------------------------------------------------- #
def value_kind(tag: int) -> str:
    nibble = tag & 0xF
    if nibble == 0x4:
        return "float"
    if nibble == 0xE:
        return "string"
    return "int"


def encode_value(tag: int, value) -> bytes:
    kind = value_kind(tag)
    if kind == "float":
        return struct.pack(">f", value)
    if kind == "string":
        return value.encode("latin-1") + b"\0"
    return struct.pack(">I", int(value) & 0xFFFFFFFF)


def decode_value(tag: int, raw: bytes):
    kind = value_kind(tag)
    if kind == "float" and len(raw) == 4:
        return struct.unpack(">f", raw)[0]
    if kind == "string":
        return raw.split(b"\0", 1)[0].decode("latin-1")
    if len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    return raw


def encode_tlvs(values: dict) -> bytes:
    """6-byte records: BE16 tag, BE32 value (integers and floats only)."""
    return b"".join(struct.pack(">H", tag) + encode_value(tag, v) for tag, v in values.items())


def decode_tlvs(data: bytes) -> dict:
    out = {}
    for i in range(0, len(data) - 5, 6):
        tag = struct.unpack_from(">H", data, i)[0]
        out[tag] = decode_value(tag, data[i + 2:i + 6])
    return out


# --------------------------------------------------------------------------- #
# Replies
# --------------------------------------------------------------------------- #
@dataclass
class ScanStatus:
    """Reply to POLL_SCAN when a scan is ready."""
    count: int           # scans completed in this session
    single_types: int    # DataType bits available for this scan
    averaged_types: int  # DataType bits available as averaged (co-added) data
    run_id: int
    raw: bytes = field(repr=False, default=b"")

    @classmethod
    def from_reply(cls, reply: Frame) -> "ScanStatus":
        return cls(reply.u32(4), reply.u32(8), reply.u32(12), reply.u32(16), reply.body)


@dataclass
class Chunk:
    """One message on EP 0x85: [cmd][index][status][?][BE32 payload length] + payload."""
    cmd: int
    index: int
    status: int
    payload: bytes

    @classmethod
    def decode(cls, raw: bytes) -> "Chunk":
        if len(raw) < 8:
            raise ProtocolError(f"short chunk: {raw.hex()}")
        length = struct.unpack_from(">I", raw, 4)[0]
        if len(raw) != 8 + length:
            raise ProtocolError(f"chunk announces {length} bytes but carries {len(raw) - 8}")
        return cls(raw[0], raw[1], raw[2], raw[8:])
