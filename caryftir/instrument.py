
"""
High-level control of the Cary 630.

    with CaryFTIR.open() as ftir:          # connects
        ftir.start()
        scan = ftir.next_scan()
        ir, laser = scan.raw.ir, scan.raw.laser
"""
import logging
import time
from dataclasses import dataclass, field

from .data import Block, RawData, decode_raw
from .health import HealthMonitor, InstrumentShutdown
from .processing import ProcessingSettings, SingleBeamAverager, Spectrum
from .protocol import (Chunk, Cmd, Control, DataType, FrameType, ProtocolError, ScanStatus, Status, Tag,
                       ValueList, connect_msg, control_msg, decode_tlvs, decode_value, encode_tlvs,
                       encode_value, get_value_msg, message, read_block_msg, read_value_list_msg,
                       request_data_types_msg, select_block_msg, set_value_msg, set_values_msg,
                       transaction_msg)
from .transport import Link, UsbEndpoints

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.109  # seconds between scan polls; the interval the original software uses


class CommandError(ProtocolError):
    pass


@dataclass
class InterferogramSettings:
    resolution: int = 4        # cm-1: 1, 2, 4, 8 or 16
    phase_points: int = 512
    left_pad: int = 128
    right_pad: int = 200


@dataclass
class SpectrumSettings:
    """Settings for spectra computed by the instrument (DataType.SINGLE_BEAM etc.)."""
    apodization: int = 1
    zero_fill: int = 0
    offset_correction: int = 0
    phase_correction: int = 1
    phase_apodization: int = 1


@dataclass
class Scan:
    index: int
    status: ScanStatus
    info: dict                                     # Tag -> value, from the SCAN_INFO block
    blocks: dict = field(default_factory=dict)     # DataType -> Block
    averaged: dict = field(default_factory=dict)   # DataType -> Block (co-added data)
    health: list = field(default_factory=list)     # health.Finding: warnings and errors for this scan

    @property
    def raw(self) -> RawData:
        return decode_raw(self.blocks[DataType.RAW].payload)


def _bits(mask: int):
    bit = 1
    while bit <= mask:
        if mask & bit:
            yield bit
        bit <<= 1


class CaryFTIR:
    def __init__(self, link: Link, health: HealthMonitor = None):
        self.link = link
        self.health = health or HealthMonitor()  # checks every scan; see health.py
        self._endpoints = None
        self.connected = False
        self.scanning = False
        self._scan_index = 0
        self._run_id = 0
        self._transaction = 0
        self._single_types = DataType.RAW
        self._averaged_types = DataType(0)
        self.available_single = 0
        self.available_averaged = 0
        self.scan_info = {}
        self.system_info = {}

    @classmethod
    def open(cls, bus: int = None, address: int = None, port_path: str = None, **usb_kwargs) -> "CaryFTIR":
        """
        Open an instrument. With one connected, no arguments are needed. With several, choose by
        `port_path` (stable while it stays in the same USB socket) or by `bus` and `address`
        (change when re-plugged). find_instruments() lists them.
        """
        endpoints = UsbEndpoints(bus=bus, address=address, port_path=port_path, **usb_kwargs)
        inst = cls(Link(endpoints))
        inst._endpoints = endpoints
        return inst

    def __enter__(self) -> "CaryFTIR":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.connected:
                self.disconnect()
        finally:
            self.close()

    def close(self) -> None:
        if self._endpoints is not None:
            self._endpoints.close()
            self._endpoints = None

    # ------------------------------------------------------------------ #
    # Basic commands
    # ------------------------------------------------------------------ #
    def _command(self, body: bytes, check: bool = True):
        reply = self.link.request(body)
        if check and (reply.type != FrameType.REPLY or reply.cmd != body[0] or reply.status != Status.OK):
            raise CommandError(f"command 0x{body[0]:02x} failed: {reply}")
        return reply

    def _next_transaction(self) -> int:
        self._transaction = (self._transaction + 1) & 0xFF
        return self._transaction

    def get_value(self, tag: int):
        reply = self._command(get_value_msg(tag))
        length = reply.u16(8)
        value = decode_value(tag, reply.body[16:16 + length])
        log.debug("value 0x%04x = %r", tag, value)
        return value

    def set_value(self, tag: int, value, flag: int = 1) -> None:
        """Set one value. `flag` is message bytes 12-15; observed as 0 or 1, meaning unknown."""
        log.debug("set value 0x%04x = %r (flag %d)", tag, value, flag)
        self._command(set_value_msg(tag, encode_value(tag, value), flag))

    def set_values(self, values: dict) -> None:
        """Set several integer/float values in one message."""
        log.debug("set values %s", {f"0x{tag:04x}": v for tag, v in values.items()})
        self._command(set_values_msg(encode_tlvs(values), self._next_transaction()))

    def control(self, control: int, data: bytes = bytes(4)) -> None:
        log.debug("control 0x%02x", control)
        self._command(control_msg(control, data))

    def _read_value_list(self, value_list: int) -> dict:
        announce = self.link.request(read_value_list_msg(value_list, addr=self._scan_index))
        if announce.type != FrameType.IN_DATA:
            raise CommandError(f"value list 0x{value_list:04x}: {announce}")
        return decode_tlvs(Chunk.decode(self.link.read_data(announce.u32(0))).payload)

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        self.link.reset()
        self._command(connect_msg())
        self._command(message(Cmd.CONNECT_CONFIRM))
        types = self._command(message(Cmd.GET_DATA_TYPES))
        self.available_single, self.available_averaged = types.u32(4), types.u32(8)
        self._scan_index = 0
        self.scan_info = self._read_value_list(ValueList.SCAN_INFO)
        self.system_info = self._read_value_list(ValueList.SYSTEM_INFO)
        self.connected = True
        log.info("connected; instrument offers single 0x%04x, averaged 0x%04x",
                 self.available_single, self.available_averaged)

    def disconnect(self) -> None:
        if self.scanning:
            self.stop()
        self._command(message(Cmd.DISCONNECT))
        self.connected = False
        log.info("disconnected")

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    def configure(self, interferogram: InterferogramSettings, spectrum: SpectrumSettings,
                  average_count: int) -> None:
        """Send the measurement settings (one message, longer than 60 bytes, so via EP 0x06)."""
        self.set_values({
            Tag.RESOLUTION: interferogram.resolution,
            Tag.PHASE_POINTS: interferogram.phase_points,
            Tag.LEFT_PAD: interferogram.left_pad,
            Tag.RIGHT_PAD: interferogram.right_pad,
            Tag.AVERAGE_COUNT: average_count,
            Tag.APODIZATION: spectrum.apodization,
            Tag.ZERO_FILL: spectrum.zero_fill,
            Tag.OFFSET_CORRECTION: spectrum.offset_correction,
            Tag.PHASE_CORRECTION: spectrum.phase_correction,
            Tag.PHASE_APODIZATION: spectrum.phase_apodization,
        })

    def request_data(self, single: int, averaged: int = 0) -> None:
        """Choose which blocks the instrument delivers per scan."""
        self._command(request_data_types_msg(single, averaged))
        self._single_types, self._averaged_types = DataType(single), DataType(averaged)
        log.debug("requested data types: single 0x%04x, averaged 0x%04x", single, averaged)

    def new_run(self, mode: int = 1) -> int:
        """Start a new run id; scans report the run they belong to."""
        self._run_id += 1
        self.set_values({Tag.RUN_ID: self._run_id, Tag.RUN_MODE: mode})
        log.info("run %d started", self._run_id)
        return self._run_id

    def refresh_info(self, timeout: float = 10.0) -> None:
        """Ask the instrument to refresh its information and wait until it has."""
        transaction = self._next_transaction()
        self._command(transaction_msg(Cmd.REFRESH_INFO, transaction))
        deadline = time.monotonic() + timeout
        polls = 0
        while True:
            reply = self._command(transaction_msg(Cmd.POLL_TRANSACTION, transaction), check=False)
            polls += 1
            if reply.status == Status.OK:
                log.debug("instrument info refreshed after %d polls", polls)
                return
            if reply.status != Status.BUSY:
                raise CommandError(f"info refresh failed: {reply}")
            if time.monotonic() > deadline:
                raise TimeoutError("info refresh did not finish")
            time.sleep(POLL_INTERVAL)

    def start(self, interferogram: InterferogramSettings = None, spectrum: SpectrumSettings = None,
              average_count: int = 1, single: int = DataType.RAW, averaged: int = 0) -> int:
        """Configure and start continuous scanning. Returns the run id."""
        interferogram, spectrum = interferogram or InterferogramSettings(), spectrum or SpectrumSettings()
        log.info("starting: %d cm-1, %d scan(s) averaged, data single 0x%04x / averaged 0x%04x",
                 interferogram.resolution, average_count, single, averaged)
        log.debug("settings: %s, %s", interferogram, spectrum)
        self.set_value(Tag.SCAN_MODE, 4)
        self.configure(interferogram, spectrum, average_count)
        # Reset to 0 by the original software before every start; purpose not fully known.
        self.set_value(Tag.INTERFEROGRAM_PEAK, 0, flag=0)
        self.set_value(Tag.UNKNOWN_0241, 0, flag=0)
        self.set_value(Tag.UNKNOWN_0221, 0)
        self.request_data(single, averaged)
        run_id = self.new_run()
        self.control(Control.START_SCANNING)
        self.scanning = True
        self.refresh_info()
        return run_id

    def stop(self) -> None:
        self.control(Control.STOP_SCANNING)
        self.scanning = False
        log.info("scanning stopped")

    def poll_scan(self) -> ScanStatus:
        """Ask once whether a scan is ready; None while the instrument is still scanning."""
        reply = self._command(message(Cmd.POLL_SCAN), check=False)
        if reply.type != FrameType.REPLY or reply.cmd != Cmd.POLL_SCAN:
            raise CommandError(f"unexpected reply to POLL_SCAN: {reply}")
        if reply.status == Status.SCANNING:
            return None
        if reply.status != Status.OK:
            raise CommandError(f"POLL_SCAN failed: {reply}")
        return ScanStatus.from_reply(reply)

    def next_scan(self, timeout: float = 10.0, poll_interval: float = POLL_INTERVAL) -> Scan:
        """Wait for the next scan, download its blocks and release it."""
        deadline = time.monotonic() + timeout
        status = self.poll_scan()
        while status is None:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no scan within {timeout} s")
            time.sleep(poll_interval)
            status = self.poll_scan()
        if status.count != self._scan_index + 1:
            log.warning("instrument reports %d scans, host expected %d", status.count, self._scan_index + 1)

        info = {}
        if status.single_types & DataType.SCAN_INFO:
            info = decode_tlvs(self._read_block(DataType.SCAN_INFO).payload)
        scan = Scan(self._scan_index, status, info)
        for bit in _bits(status.single_types & self._single_types & ~DataType.SCAN_INFO):
            scan.blocks[DataType(bit)] = self._read_block(bit)
        for bit in _bits(status.averaged_types & self._averaged_types):
            scan.averaged[DataType(bit)] = self._read_block(bit, averaged=True)

        # The instrument does not start the next scan until this one is released.
        self._command(message(Cmd.RELEASE_SCAN))
        self._scan_index += 1
        log.debug("scan %d (run %d): %s", scan.index, status.run_id,
                  ", ".join(f"{DataType(t).name} {len(b.payload)} B" for t, b in {**scan.blocks, **scan.averaged}.items()))
        scan.health = self.health.check(scan)
        shutdown = [f for f in scan.health if f.check == "shutdown"]
        if shutdown:
            raise InstrumentShutdown(shutdown[0].message)
        return scan

    def measure_single_beam(self, scans: int, settings: ProcessingSettings = None,
                            max_rejected: int = None) -> Spectrum:
        """Collect `scans` good scans (after start()) and return their average single beam."""
        averager = SingleBeamAverager(settings or ProcessingSettings())
        limit = max_rejected if max_rejected is not None else max(3, scans)
        while averager.count < scans:
            scan = self.next_scan()
            averager.add_raw(scan.raw, scan.info)
            if averager.rejected > limit:
                raise RuntimeError(f"{averager.rejected} scans rejected; check the instrument and sample")
        log.info("single beam from %d scans (%d rejected), laser wavenumber %.2f cm-1",
                 averager.count, averager.rejected, sum(averager.laser_wavenumbers) / averager.count)
        return averager.result()

    def _read_block(self, data_type: int, averaged: bool = False) -> Block:
        """Select a block, read it chunk by chunk until a chunk is marked DONE, release it."""
        selected = self._command(select_block_msg(data_type, averaged))
        count, element_size = selected.u32(4), selected.u32(8)
        expected = count * element_size
        payload = bytearray()
        chunks = 0
        while True:
            announce = self.link.request(read_block_msg(self._scan_index))
            if announce.type != FrameType.IN_DATA:
                raise CommandError(f"READ_BLOCK 0x{data_type:x}: {announce}")
            chunk = Chunk.decode(self.link.read_data(announce.u32(0)))
            payload += chunk.payload
            chunks += 1
            if chunk.status == Status.DONE:
                break
            if len(payload) >= expected:
                raise ProtocolError(f"block 0x{data_type:x}: {len(payload)} bytes and no DONE chunk")
        self._command(message(Cmd.RELEASE_BLOCK))
        if chunks > 1:
            log.debug("block 0x%x: %d bytes in %d chunks", data_type, len(payload), chunks)
        if len(payload) != expected:
            log.warning("block 0x%x: got %d bytes, expected %d", data_type, len(payload), expected)
        return Block(data_type, element_size, bytes(payload), averaged)

    # ------------------------------------------------------------------ #
    # Information
    # ------------------------------------------------------------------ #
    def info(self) -> dict:
        return {
            "serial_number": self.get_value(Tag.SERIAL_NUMBER),
            "laser_wavenumber": self.get_value(Tag.LASER_WAVENUMBER),
            "ir_gain": self.get_value(Tag.IR_GAIN),
            "laser_gain": self.get_value(Tag.LASER_GAIN),
        }
