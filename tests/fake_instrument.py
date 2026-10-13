
"""
A simulated Cary 630 at the USB-endpoint level, modelled on USB captures of the original
software. It enforces the rules whose violation hangs the real instrument (e.g. selecting a
new block before the previous one is released).
"""
import struct

import numpy as np

from caryftir.processing import LASER_WAVENUMBER
from caryftir.protocol import (EP_COMMAND_OUT, EP_DATA_IN, EP_DATA_OUT, EP_REPLY_IN, Cmd, DataType,
                               FrameType, Status, decode_frame, encode_frame, encode_tlvs)

RAW_RECORDS = 58515  # 4 cm-1, as in the captures
LINES = (1000.0, 2000.0, 3000.0, 3800.0)  # cm-1, equal strength and width
WIDTH = 8.0                                 # Gaussian standard deviation, cm-1


def synthetic_scan(samples=29258, zpd_fraction=0.13, samples_per_period=11.8, wobble=0.03, peak=1.0,
                   laser_wavenumber=LASER_WAVENUMBER):
    """IR and laser samples of a scan with known spectral lines, for a mirror moving at uneven speed."""
    t = np.arange(samples, dtype=float)
    step = 1 / (laser_wavenumber * samples_per_period)  # cm of path difference per sample
    x = np.cumsum(1 + wobble * np.sin(2 * np.pi * t / 4000)) * step
    x -= x[int(zpd_fraction * samples)]
    laser = np.cos(2 * np.pi * laser_wavenumber * x)
    ir = peak * sum(np.exp(-2 * (np.pi * WIDTH * x) ** 2) * np.cos(2 * np.pi * s * x) for s in LINES)
    return ir + 0.25, laser


def synthetic_raw_records() -> bytes:
    """synthetic_scan() encoded as RAW records: IR on even class bytes, laser on odd, 24-bit values."""
    ir, laser = synthetic_scan()
    values = np.empty(RAW_RECORDS, dtype=np.int64)
    scale = 1.5e6  # keeps the IR peak (4.25) inside the signed 24-bit range
    values[0::2], values[1::2] = np.round(ir * scale), np.round(laser[:RAW_RECORDS // 2] * scale)
    records = np.zeros((RAW_RECORDS, 4), np.uint8)
    records[:, 0] = np.where(np.arange(RAW_RECORDS) % 2 == 0, 0x02, 0x03)
    for i, shift in enumerate((16, 8, 0)):
        records[:, i + 1] = (values >> shift) & 0xFF
    return records.tobytes()


class FakeInstrument:
    def __init__(self, polls_per_scan: int = 3, chunk_size: int = 0x7FFF):
        self.polls_per_scan = polls_per_scan
        self.chunk_size = chunk_size        # bytes per READ_BLOCK chunk
        self.sent = []                      # application messages received, in order
        self._replies = []
        self._data_in = b""
        self._pending_out = None            # (seq, remaining bytes) for an EP 0x06 transfer
        self.values = {0x102E: b"MY2446CU14\0", 0x3184: struct.pack(">f", 7659.79)}
        self.requested = (0, 0)
        self.scanning = False
        self.completed = 0
        self._polls = 0
        self._scan_polls = 0
        self.scan_pending = False
        self.selected = None                # (data_type, remaining payload)
        self.object_open = False
        # status values reported in every scan's info, as in the captures; tests may change them
        self.status = {0x1901: 0, 0x1911: 0, 0x1944: 52.0, 0x1954: 51.875, 0x1964: 59.6, 0x1974: 40.0,
                       0x1984: 49.25, 0x1994: 999.0, 0x19A4: 999.0, 0x19B4: 999.0, 0x1934: 999.0,
                       0x19E4: 2.70, 0x19F4: 1.86}

    # -- endpoint interface ------------------------------------------------
    def write(self, endpoint: int, data: bytes) -> None:
        if endpoint == EP_DATA_OUT:
            seq, need, buf = self._pending_out
            buf += data
            if len(buf) >= need:
                self._pending_out = None
                self._handle(seq, buf[:need])
            else:
                self._pending_out = (seq, need, buf)
            return
        assert endpoint == EP_COMMAND_OUT
        frame = decode_frame(data)
        if frame.type == FrameType.RESET:
            self._replies.append(encode_frame(FrameType.RESET, frame.seq, bytes([0x07, 0xFF, 0x00, 0x01])))
        elif frame.type == FrameType.OUT_DATA:
            self._pending_out = (frame.seq, struct.unpack(">I", frame.body)[0], b"")
            self._replies.append(encode_frame(FrameType.OUT_DATA, frame.seq))
        elif frame.type == FrameType.COMMAND:
            self._handle(frame.seq, frame.body)
        else:
            self._replies.append(encode_frame(FrameType.REJECT, frame.seq))

    def read(self, endpoint: int, size: int, timeout_ms: int = None) -> bytes:
        if endpoint == EP_REPLY_IN:
            return self._replies.pop(0)
        assert endpoint == EP_DATA_IN
        if not self._data_in:
            raise IOError("timed out")
        n = min(size, len(self._data_in))
        out, self._data_in = self._data_in[:n], self._data_in[n:]
        return out

    # -- firmware model ------------------------------------------------------
    def _reply(self, seq, cmd, status=Status.OK, fields=(), length=16, tail=b""):
        body = bytes([cmd, 0, status, 0]) + b"".join(struct.pack(">I", f) for f in fields)
        self._replies.append(encode_frame(FrameType.REPLY, seq, body.ljust(length, b"\0") + tail))

    def _send_data(self, seq, cmd, index, status, payload):
        msg = bytes([cmd, index, status, 0x10]) + struct.pack(">I", len(payload)) + payload
        self._replies.append(encode_frame(FrameType.IN_DATA, seq, struct.pack(">I", len(msg))))
        self._data_in += msg

    def _block(self, data_type):
        if data_type == DataType.SCAN_INFO:
            info = {0x31: self.values.get(0x31, 0), 0x141: self.values.get(0x141, 1),
                    # laser values consistent with the synthetic scan: 7633.588 cm-1 at a block temperature of 49.25 deg C
                    0x3184: LASER_WAVENUMBER + 0.47 * 49.25, 0x3194: -0.47, **self.status}
            return encode_tlvs(info).ljust(600, b"\0"), 1
        if data_type == DataType.RAW:
            return synthetic_raw_records(), 4
        if data_type == DataType.SINGLE_BEAM:
            return b"".join(struct.pack(">f", i / 10) for i in range(1000)), 4
        raise AssertionError(f"no model for data type 0x{data_type:x}")

    def _handle(self, seq, body):
        self.sent.append(body)
        cmd = body[0]
        if cmd == Cmd.GET_DATA_TYPES:
            self._reply(seq, cmd, fields=(0x4111, 0x0110))
        elif cmd == Cmd.READ_VALUE_LIST:
            self._send_data(seq, cmd, body[1], Status.DONE, encode_tlvs({0x11: 0x2BD2C, 0x31: 0}))
        elif cmd == Cmd.REQUEST_DATA_TYPES:
            self.requested = struct.unpack_from(">II", body, 4)
            self._reply(seq, cmd)
        elif cmd == Cmd.SET_VALUES:
            size = struct.unpack_from(">H", body, 4)[0]
            for i in range(16, 16 + size, 6):
                self.values[struct.unpack_from(">H", body, i)[0]] = struct.unpack_from(">I", body, i + 2)[0]
            self._reply(seq, cmd)
        elif cmd == Cmd.GET_VALUE:
            tag = struct.unpack_from(">I", body, 4)[0]
            value = self.values.get(tag, bytes(4))
            self._reply(seq, cmd, fields=(0, len(value) << 16, 0), length=16, tail=value)
        elif cmd == Cmd.CONTROL:
            if body[4] == 0x10:
                self.scanning = True
            elif body[4] == 0x11:
                self.scanning = False
            self._reply(seq, cmd)
        elif cmd == Cmd.POLL_TRANSACTION:
            self._polls += 1
            self._reply(seq, cmd, Status.OK if self._polls > 2 else Status.BUSY)
        elif cmd == Cmd.POLL_SCAN:
            self._next_scan(seq)
        elif cmd == Cmd.SELECT_BLOCK:
            assert self.scan_pending, "SELECT_BLOCK without a scan"
            assert not self.object_open, "SELECT_BLOCK before RELEASE_BLOCK -> real instrument hangs"
            data_type = struct.unpack_from(">I", body, 4)[0]
            payload, element = self._block(data_type)
            self.selected = (data_type, payload)
            self.object_open = True
            self._reply(seq, cmd, fields=(len(payload) // element, element, 1))
        elif cmd == Cmd.READ_BLOCK:
            assert body[1] == (self.completed - 1) & 0xFF, f"read scan {body[1]}, latest is {self.completed - 1}"
            data_type, payload = self.selected
            chunk, rest = payload[:self.chunk_size], payload[self.chunk_size:]
            self.selected = (data_type, rest)
            self._send_data(seq, cmd, body[1], Status.DONE if not rest else Status.OK, chunk)
        elif cmd == Cmd.RELEASE_BLOCK:
            assert self.object_open
            self.object_open = False
            self._reply(seq, cmd)
        elif cmd == Cmd.RELEASE_SCAN:
            assert self.scan_pending and not self.object_open
            self.scan_pending = False
            self._reply(seq, cmd)
        else:
            self._reply(seq, cmd)

    def _next_scan(self, seq):
        if not self.scanning:
            self._reply(seq, Cmd.POLL_SCAN, Status.SCANNING, length=48)
            return
        if not self.scan_pending:
            self._scan_polls += 1
            if self._scan_polls <= self.polls_per_scan:
                self._reply(seq, Cmd.POLL_SCAN, Status.SCANNING, length=48)
                return
            self._scan_polls = 0
            self.completed += 1
            self.scan_pending = True
        single = DataType.SCAN_INFO | (self.requested[0] & 0x0111)
        averaged = self.requested[1] & 0x0110
        self._reply(seq, Cmd.POLL_SCAN, fields=(self.completed, single, averaged, self.values.get(0x31, 0)),
                    length=48)
