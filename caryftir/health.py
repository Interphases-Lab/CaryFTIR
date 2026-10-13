
"""
Instrument health: the warnings and errors the original software reports.

Two mechanisms, both evaluated for every scan:

* The instrument signals a shutdown itself, through bits in Tag.SHUTDOWN_FLAGS. The original
  software then closes the connection. Here, next_scan() raises InstrumentShutdown.
* Everything else is a host-side check: status values from the scan's info (temperatures) and
  values computed from the raw data (energy, laser) are compared against limits. The defaults
  are the original software's factory configuration. Results are a warning ("marginal") or an
  error ("critical"); scanning continues.

    with CaryFTIR.open() as ftir:
        ftir.start()
        scan = ftir.next_scan()
        for finding in scan.health:
            print(finding)
"""
import logging
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np

from .data import decode_raw
from .protocol import DataType, Tag

log = logging.getLogger(__name__)

NO_SENSOR = 999.0  # temperature slots without a sensor read 999


class Level(IntEnum):
    OK = 0
    WARNING = 1  # "marginal" in the original software
    ERROR = 2    # "critical"


@dataclass(frozen=True)
class Limits:
    """Warning and error thresholds; None disables a side."""
    warning_low: float = None
    warning_high: float = None
    error_low: float = None
    error_high: float = None

    def level(self, value: float) -> Level:
        if self.error_low is not None and value < self.error_low:
            return Level.ERROR
        if self.error_high is not None and value > self.error_high:
            return Level.ERROR
        if self.warning_low is not None and value < self.warning_low:
            return Level.WARNING
        if self.warning_high is not None and value > self.warning_high:
            return Level.WARNING
        return Level.OK


# Factory limits of the original software. Its "overheating" error is the block temperature.
DEFAULT_LIMITS = {
    "block_temperature": Limits(warning_low=-10, warning_high=70, error_low=-20, error_high=80),
    "detector_temperature": Limits(warning_low=38, warning_high=42, error_low=36, error_high=44),
    "energy": Limits(warning_low=22000, warning_high=28000, error_low=20000, error_high=30000),
    "laser": Limits(warning_low=5500, warning_high=15000, error_low=3000, error_high=17000),
}

# Present in the original configuration but switched off there; add to `limits` to use them.
OPTIONAL_LIMITS = {
    "cpu_temperature": Limits(warning_low=43, warning_high=44, error_low=42, error_high=45),
    "ir_temperature": Limits(warning_low=18, warning_high=47, error_low=15, error_high=50),
    "power_temperature": Limits(warning_low=18, warning_high=47, error_low=15, error_high=50),
}

UNITS = {"energy": "", "laser": "", "source_voltage": " V", "source_current": " A"}

STATUS_READINGS = {
    "cpu_temperature": Tag.CPU_TEMPERATURE,
    "power_temperature": Tag.POWER_TEMPERATURE,
    "ir_temperature": Tag.IR_TEMPERATURE,
    "detector_temperature": Tag.DETECTOR_TEMPERATURE,
    "block_temperature": Tag.BLOCK_TEMPERATURE,
    "source_voltage": Tag.SOURCE_VOLTAGE,
    "source_current": Tag.SOURCE_CURRENT,
}


class InstrumentShutdown(RuntimeError):
    """The instrument signalled that it is shutting down."""


@dataclass
class Finding:
    check: str
    level: Level
    value: float
    message: str

    def __str__(self) -> str:
        return f"{self.level.name}: {self.message}"


def readings(scan) -> dict:
    """Values the checks look at: status temperatures, source values, energy and laser."""
    values = {}
    for name, tag in STATUS_READINGS.items():
        value = scan.info.get(tag)
        if isinstance(value, float) and value != NO_SENSOR:
            values[name] = value
    if DataType.RAW in scan.blocks:
        raw = decode_raw(scan.blocks[DataType.RAW].payload)
        if len(raw.ir) and len(raw.laser):
            # 24-bit peak magnitudes / 256, the scale the original software reports
            values["energy"] = float(np.abs(raw.ir).max()) / 256
            values["laser"] = float(np.abs(raw.laser).max()) / 256
    return values


@dataclass
class HealthMonitor:
    """Checks scans; logs each problem when it appears, changes level or clears."""
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    _accessory: int = None
    _last: dict = field(default_factory=dict)

    def check(self, scan) -> list:
        findings = []
        flags = scan.info.get(Tag.SHUTDOWN_FLAGS, 0) or 0
        if flags & 0x01:
            findings.append(Finding("shutdown", Level.ERROR, flags, "instrument is shutting down"))
        if flags & 0x10:
            findings.append(Finding("shutdown", Level.ERROR, flags, "battery critical, instrument is shutting down"))

        accessory = scan.info.get(Tag.SAMPLING_ACCESSORY)
        if self._accessory is not None and accessory is not None and accessory != self._accessory:
            findings.append(Finding("accessory", Level.WARNING, accessory,
                                    f"sampling accessory changed ({self._accessory} -> {accessory})"))
        if accessory is not None:
            self._accessory = accessory

        values = readings(scan)
        for name, limits in self.limits.items():
            if name not in values:
                continue
            level = limits.level(values[name])
            if level:
                label = name.replace("_", " ")
                unit = UNITS.get(name, " °C")
                bound = self._bound(limits, values[name], level)
                findings.append(Finding(name, level, values[name], f"{label} {values[name]:.1f}{unit} ({bound})"))

        self._log(findings)
        return findings

    @staticmethod
    def _bound(limits: Limits, value: float, level: Level) -> str:
        low = limits.error_low if level == Level.ERROR else limits.warning_low
        high = limits.error_high if level == Level.ERROR else limits.warning_high
        if low is not None and value < low:
            return f"below {low:g}"
        return f"above {high:g}"

    def _log(self, findings: list) -> None:
        current = {f.check: f for f in findings}
        for check, f in current.items():
            if self._last.get(check) != f.level:
                (log.error if f.level == Level.ERROR else log.warning)("%s", f.message)
        for check in set(self._last) - set(current):
            log.info("%s back to normal", check.replace("_", " "))
        self._last = {c: f.level for c, f in current.items()}
