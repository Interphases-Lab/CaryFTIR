
"""
From raw scans to spectra.

Based on the processing of the bachelor thesis prototype credited in the README (zero-crossing
resampling and a Mertz-phase-corrected FFT), with changes:

* scans with a misplaced centerburst, an outlying peak height or a weak laser are rejected
  (the first scan of a session is always such a scan; the original code crashed on it);
* the interferogram is apodized (Happ-Genzel by default) and the phase region is windowed;
* the IR signal is interpolated at the laser crossings with a cubic spline instead of linearly;
* the wavenumber axis is exact: spectra are put on the grid of the original software
  (k * 2 * 7633.588 / N cm-1), optionally corrected for the laser temperature (see below);
* absorbance is not smoothed, and regions without enough light are NaN instead of removed.

Laser temperature correction (an educated guess, on by default): the reference laser's
wavenumber is taken as  LASER_WAVENUMBER + LASER_TEMPERATURE_COEFFICIENT * BLOCK_TEMPERATURE
from each scan's info (about 7636.6 cm-1 on the tested instrument), and the spectrum is
resampled onto the standard grid. Atmospheric CO2 in captured backgrounds cannot confirm it: at 4 cm-1 the CO2 Q-branch
peak is not known to better than a few tenths of a cm-1 (667.79 with, 667.52 without the
correction; 667.38 literature band centre). Without plausible values 7633.588 cm-1 is used.

    with CaryFTIR.open() as ftir:
        ftir.start()
        background = ftir.measure_single_beam(scans=16)
        input("Load the sample, then press Enter")
        sample = ftir.measure_single_beam(scans=16)
    absorbance = sample.absorbance(background)
    absorbance.to_csv("sample.csv")         # or .to_parquet(), .to_df() for a polars DataFrame
"""
import logging
from dataclasses import dataclass, field

import numpy as np
import polars as pl
from scipy.interpolate import CubicSpline

from .data import RawData
from .protocol import Tag

log = logging.getLogger(__name__)

LASER_WAVENUMBER = 7633.588  # cm-1; defines the output grid and is the fallback laser wavenumber


def laser_wavenumber_from_info(info: dict) -> float:
    """Temperature-corrected laser wavenumber from a scan's info, or None (educated guess, see module doc)."""
    try:
        offset = info[Tag.LASER_WAVENUMBER]
        slope = info[Tag.LASER_TEMPERATURE_COEFFICIENT]
        temperature = info[Tag.BLOCK_TEMPERATURE]
    except (KeyError, TypeError):
        return None
    sigma = offset + slope * temperature
    if not -20 < temperature < 120 or abs(sigma / LASER_WAVENUMBER - 1) > 0.005:
        log.warning("implausible laser values (%s, %s, %s); using %.3f cm-1",
                    offset, slope, temperature, LASER_WAVENUMBER)
        return None
    return sigma


# --------------------------------------------------------------------------- #
# Apodization windows, as functions of the distance from the centerburst x in [0, 1]
# --------------------------------------------------------------------------- #
def _norton_beer(*c):
    return lambda x: sum(ci * (1 - x ** 2) ** i for i, ci in enumerate(c))


WINDOWS = {
    "none": lambda x: np.ones_like(x),
    "triangular": lambda x: 1 - x,
    "happ-genzel": lambda x: 0.54 + 0.46 * np.cos(np.pi * x),
    "hann": lambda x: 0.5 + 0.5 * np.cos(np.pi * x),
    "norton-beer-weak": _norton_beer(0.384093, -0.087577, 0.703484),
    "norton-beer-medium": _norton_beer(0.152442, -0.136176, 0.983734),
    "norton-beer-strong": _norton_beer(0.045335, 0.0, 0.554883, 0.0, 0.399782),
}


@dataclass
class ProcessingSettings:
    apodization: str = "happ-genzel"
    phase_points: int = 512             # double-sided points around the centerburst used for the phase
    phase_apodization: str = "triangular"
    zero_fill: int = 1                  # FFT length = next power of two >= zero_fill * interferogram length
    interpolation: str = "cubic"        # "cubic" or "linear"
    laser_wavenumber: float = LASER_WAVENUMBER  # output grid, and laser wavenumber if not corrected
    temperature_correction: bool = True  # educated guess, see laser_wavenumber_from_info


WAVENUMBER_COLUMN = "wavenumber_cm-1"


@dataclass
class Spectrum:
    wavenumbers: np.ndarray
    values: np.ndarray
    quantity: str = "single_beam"  # "single_beam", "transmittance" or "absorbance"; the value column name

    def crop(self, low: float = 650.0, high: float = 4000.0) -> "Spectrum":
        keep = (self.wavenumbers >= low) & (self.wavenumbers <= high)
        return Spectrum(self.wavenumbers[keep], self.values[keep], self.quantity)

    def to_df(self) -> pl.DataFrame:
        """Columns `wavenumber_cm-1` and the quantity (e.g. `absorbance`); NaN (too little light) becomes null."""
        return pl.DataFrame({WAVENUMBER_COLUMN: self.wavenumbers, self.quantity: self.values}).fill_nan(None)

    def to_csv(self, path) -> None:
        """Write `to_df()` as CSV; nulls are empty fields."""
        self.to_df().write_csv(path)
        log.info("saved %s (%d points) to %s", self.quantity, len(self.values), path)

    def to_parquet(self, path) -> None:
        """Write `to_df()` as Parquet."""
        self.to_df().write_parquet(path)
        log.info("saved %s (%d points) to %s", self.quantity, len(self.values), path)

    def transmittance(self, background: "Spectrum", min_light: float = 0.02) -> "Spectrum":
        """Sample / background; NaN where the background has less than `min_light` of its maximum."""
        if not np.array_equal(self.wavenumbers, background.wavenumbers):
            raise ValueError("sample and background are on different wavenumber grids")
        bg = background.values
        with np.errstate(divide="ignore", invalid="ignore"):
            t = self.values / bg
        t[(bg < min_light * np.nanmax(bg)) | (self.values <= 0)] = np.nan
        return Spectrum(self.wavenumbers, t, "transmittance")

    def absorbance(self, background: "Spectrum", min_light: float = 0.02) -> "Spectrum":
        t = self.transmittance(background, min_light)
        return Spectrum(t.wavenumbers, -np.log10(t.values), "absorbance")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def split(raw: RawData):
    """IR and laser samples of one scan, trimmed to equal length (they alternate in the raw data)."""
    ir, laser = raw.ir.astype(float), raw.laser.astype(float)
    n = min(len(ir), len(laser))
    return ir[:n], laser[:n]


def laser_crossings(laser: np.ndarray) -> np.ndarray:
    """Sub-sample positions of all laser zero crossings, starting with the first rising one."""
    centered = laser - laser.mean()
    idx = np.nonzero(np.diff(np.sign(centered)))[0]
    y0, y1 = centered[idx], centered[idx + 1]
    valid = y1 != y0
    idx, y0, y1 = idx[valid], y0[valid], y1[valid]
    positions = idx - y0 / (y1 - y0)
    rising = np.nonzero(y1 > y0)[0]
    if len(rising) == 0:
        raise ValueError("no laser signal")
    return positions[rising[0]:]


def resample(ir: np.ndarray, laser: np.ndarray, method: str = "cubic") -> np.ndarray:
    """IR signal at every laser zero crossing: equal steps of half a laser wavelength."""
    positions = laser_crossings(laser)
    positions = positions[positions <= len(ir) - 1]
    if method == "linear":
        return np.interp(positions, np.arange(len(ir)), ir)
    return CubicSpline(np.arange(len(ir)), ir)(positions)


def centerburst(interferogram: np.ndarray) -> int:
    return int(np.argmax(np.abs(interferogram - interferogram.mean())))


def single_beam(interferogram: np.ndarray, settings: ProcessingSettings = None,
                laser_wavenumber: float = None) -> Spectrum:
    """
    Mertz-phase-corrected single-beam spectrum of a resampled interferogram, on the grid
    k * 2 * settings.laser_wavenumber / N. If the actual `laser_wavenumber` differs, the
    spectrum is resampled from its true axis onto that grid.
    """
    s = settings or ProcessingSettings()
    ifg = interferogram - interferogram.mean()
    n, z, half = len(ifg), centerburst(interferogram), s.phase_points // 2
    if not half <= z < n // 2:
        raise ValueError(f"centerburst at {z} of {n} points; cannot compute a single-sided spectrum")

    # Phase from a short double-sided part around the centerburst.
    seg = ifg[z - half:z + half] * WINDOWS[s.phase_apodization](np.abs(np.arange(-half, half)) / half)
    phase = np.unwrap(np.angle(np.fft.fft(np.roll(seg, -half))))

    # Mertz ramp over the double-sided part, apodization from the centerburst to the far end.
    ramp = np.clip(np.arange(n) / (2 * z), 0, 1)
    apod = WINDOWS[s.apodization](np.minimum(np.abs(np.arange(n) - z) / (n - z), 1))
    size = 2 ** int(np.ceil(np.log2(n * max(s.zero_fill, 1))))
    padded = np.zeros(size)
    padded[:n] = ifg * ramp * apod
    spectrum = np.fft.fft(np.roll(padded, -z))

    # Phase bin k sits at the same frequency as spectrum bin k * size / phase_points.
    phase_full = np.interp(np.arange(size) * s.phase_points / size, np.arange(s.phase_points), phase)
    values = (spectrum * np.exp(-1j * phase_full)).real[:size // 2]
    wavenumbers = np.arange(size // 2) * (2 * s.laser_wavenumber / size)
    if laser_wavenumber and laser_wavenumber != s.laser_wavenumber:
        true_axis = np.arange(size // 2) * (2 * laser_wavenumber / size)
        values = CubicSpline(true_axis, values, extrapolate=True)(wavenumbers)
    return Spectrum(wavenumbers, values)


# --------------------------------------------------------------------------- #
# Scan checks and averaging
# --------------------------------------------------------------------------- #
@dataclass
class SingleBeamAverager:
    """Averages single beams of good scans; rejects scans the way the original software does."""
    settings: ProcessingSettings = field(default_factory=ProcessingSettings)
    max_peak_deviation: float = 0.2   # relative to the median peak of accepted scans
    min_laser_fraction: float = 0.5   # laser amplitude relative to the median of accepted scans
    _sum: np.ndarray = None
    _wavenumbers: np.ndarray = None
    _peaks: list = field(default_factory=list)
    _lasers: list = field(default_factory=list)
    laser_wavenumbers: list = field(default_factory=list)  # laser wavenumber used per accepted scan
    count: int = 0
    rejected: int = 0

    def check(self, interferogram: np.ndarray, laser: np.ndarray) -> str:
        """Reason to reject this scan, or None."""
        z, n, half = centerburst(interferogram), len(interferogram), self.settings.phase_points // 2
        if not half <= z < n // 2:
            return f"centerburst at {z} of {n} points"
        peak = np.abs(interferogram - interferogram.mean())[z]
        laser_amp = np.percentile(laser, 99) - np.percentile(laser, 1)
        if self._peaks:
            ref = np.median(self._peaks)
            if abs(peak - ref) > self.max_peak_deviation * ref:
                return f"peak {peak:.0f} differs from {ref:.0f} by more than {self.max_peak_deviation:.0%}"
            if laser_amp < self.min_laser_fraction * np.median(self._lasers):
                return "weak laser signal"
        return None

    def add_raw(self, raw: RawData, info: dict = None) -> bool:
        """Add one scan; `info` is Scan.info, used for the laser temperature correction."""
        ir, laser = split(raw)
        return self.add(resample(ir, laser, self.settings.interpolation), laser, info)

    def add(self, interferogram: np.ndarray, laser: np.ndarray, info: dict = None) -> bool:
        reason = self.check(interferogram, laser)
        if reason:
            self.rejected += 1
            log.info("scan rejected: %s", reason)
            return False
        sigma = laser_wavenumber_from_info(info) if self.settings.temperature_correction and info else None
        sb = single_beam(interferogram, self.settings, sigma)
        self.laser_wavenumbers.append(sigma or self.settings.laser_wavenumber)
        if self._sum is None:
            self._sum, self._wavenumbers = np.zeros_like(sb.values), sb.wavenumbers
        if len(sb.values) != len(self._sum):
            self.rejected += 1
            log.info("scan rejected: FFT length %d differs from %d", len(sb.values), len(self._sum))
            return False
        self._sum += sb.values
        self._peaks.append(np.abs(interferogram - interferogram.mean()).max())
        self._lasers.append(np.percentile(laser, 99) - np.percentile(laser, 1))
        self.count += 1
        return True

    def result(self) -> Spectrum:
        if not self.count:
            raise ValueError("no accepted scans")
        return Spectrum(self._wavenumbers, self._sum / self.count)
