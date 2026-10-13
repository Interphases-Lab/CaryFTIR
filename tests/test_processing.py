
"""Processing against a synthetic scan whose spectrum is known exactly."""
import os
import tempfile
import unittest

import numpy as np
import polars as pl

from caryftir.processing import (LASER_WAVENUMBER, WINDOWS, ProcessingSettings, SingleBeamAverager, Spectrum,
                                 laser_crossings, laser_wavenumber_from_info, resample, single_beam)
from tests.fake_instrument import LINES, synthetic_scan


def peaks(spectrum: Spectrum):
    found = []
    for s in LINES:
        m = np.abs(spectrum.wavenumbers - s) < 30
        i = np.argmax(spectrum.values[m])
        found.append((spectrum.wavenumbers[m][i], spectrum.values[m][i]))
    return found


class TestSingleBeam(unittest.TestCase):
    def test_line_positions_and_heights(self):
        ir, laser = synthetic_scan()
        found = peaks(single_beam(resample(ir, laser)))
        heights = np.array([h for _, h in found])
        for (pos, _), true in zip(found, LINES):
            self.assertLess(abs(pos - true), 1.0, f"line {true} found at {pos}")
        self.assertLess(heights.max() / heights.min() - 1, 0.01)

    def test_cubic_keeps_high_wavenumber_intensity(self):
        """Line areas at 3800 vs 1000 cm-1: linear interpolation loses ~0.5%, cubic almost nothing."""
        ir, laser = synthetic_scan()

        def loss(method):
            sb = single_beam(resample(ir, laser, method), ProcessingSettings(apodization="none"))
            area = lambda s: sb.values[np.abs(sb.wavenumbers - s) < 40].sum()
            return 1 - area(3800.0) / area(1000.0)

        self.assertLess(abs(loss("cubic")), 0.001)
        self.assertGreater(loss("linear"), 0.004)

    def test_axis(self):
        ir, laser = synthetic_scan()
        sb = single_beam(resample(ir, laser))
        self.assertEqual(len(sb.values), 4096)
        self.assertAlmostEqual(sb.wavenumbers[1], 2 * LASER_WAVENUMBER / 8192)

    def test_crossings_start_rising(self):
        laser = np.cos(np.linspace(0, 20 * np.pi, 1000))
        first = int(laser_crossings(laser)[0])
        self.assertLess(laser[first], 0)
        self.assertGreater(laser[first + 1], 0)

    def test_windows(self):
        for name, w in WINDOWS.items():
            self.assertAlmostEqual(float(w(np.array(0.0))), 1.0, places=5, msg=name)
        self.assertAlmostEqual(float(WINDOWS["happ-genzel"](np.array(1.0))), 0.08)

    def test_misplaced_centerburst_raises(self):
        ir, laser = synthetic_scan(zpd_fraction=0.62)
        with self.assertRaises(ValueError):
            single_beam(resample(ir, laser))


def fitted_peak(spectrum: Spectrum, line: float) -> float:
    """Peak position between grid points (parabola through the three highest points)."""
    near = np.nonzero(np.abs(spectrum.wavenumbers - line) < 30)[0]
    i = near[np.argmax(spectrum.values[near])]
    y0, y1, y2 = spectrum.values[i - 1:i + 2]
    step = spectrum.wavenumbers[1] - spectrum.wavenumbers[0]
    return spectrum.wavenumbers[i] + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) * step


class TestLaserTemperatureCorrection(unittest.TestCase):
    INFO = {0x3184: 7659.79, 0x3194: -0.47, 0x1984: 49.25}

    def test_wavenumber_from_info(self):
        self.assertAlmostEqual(laser_wavenumber_from_info(self.INFO), 7659.79 - 0.47 * 49.25)
        self.assertIsNone(laser_wavenumber_from_info({}))
        self.assertIsNone(laser_wavenumber_from_info({**self.INFO, 0x1984: 900.0}))   # implausible temperature
        self.assertIsNone(laser_wavenumber_from_info({**self.INFO, 0x3184: 9000.0}))  # implausible wavenumber

    def test_correction_puts_lines_at_true_positions(self):
        true_laser = laser_wavenumber_from_info(self.INFO)  # ~7636.6, i.e. 0.04% above the grid's 7633.588
        ir, laser = synthetic_scan(laser_wavenumber=true_laser)
        clocked = resample(ir, laser)
        corrected = single_beam(clocked, laser_wavenumber=true_laser)
        uncorrected = single_beam(clocked)
        self.assertLess(abs(fitted_peak(corrected, 3800.0) - 3800.0), 0.2)
        self.assertGreater(abs(fitted_peak(uncorrected, 3800.0) - 3800.0), 0.6)  # expected shift ~1.5 cm-1
        np.testing.assert_array_equal(corrected.wavenumbers, uncorrected.wavenumbers)  # same output grid

    def test_averager_uses_info_unless_disabled(self):
        ir, laser = synthetic_scan()
        clocked = resample(ir, laser)
        on = SingleBeamAverager()
        on.add(clocked, laser, self.INFO)
        off = SingleBeamAverager(ProcessingSettings(temperature_correction=False))
        off.add(clocked, laser, self.INFO)
        self.assertAlmostEqual(on.laser_wavenumbers[0], 7659.79 - 0.47 * 49.25)
        self.assertEqual(off.laser_wavenumbers[0], LASER_WAVENUMBER)


class TestAveraging(unittest.TestCase):
    def test_rejects_bad_scans(self):
        avg = SingleBeamAverager()
        self.assertFalse(avg.add(*self._clocked(zpd_fraction=0.62)))   # like the first scan of a session
        for _ in range(3):
            self.assertTrue(avg.add(*self._clocked()))
        self.assertFalse(avg.add(*self._clocked(peak=0.5)))             # peak 50% low
        self.assertEqual((avg.count, avg.rejected), (3, 2))
        found = peaks(avg.result())
        self.assertLess(abs(found[0][0] - LINES[0]), 1.0)

    @staticmethod
    def _clocked(**kw):
        ir, laser = synthetic_scan(**kw)
        return resample(ir, laser), laser


class TestRatio(unittest.TestCase):
    def test_absorbance_and_dark_regions(self):
        w = np.array([1000.0, 2000.0, 3000.0])
        bg = Spectrum(w, np.array([1.0, 0.001, 2.0]))
        smp = Spectrum(w, np.array([0.1, 0.001, 2.0]))
        a = smp.absorbance(bg).values
        self.assertAlmostEqual(a[0], 1.0)
        self.assertTrue(np.isnan(a[1]))   # background below 2% of its maximum
        self.assertAlmostEqual(a[2], 0.0)
        self.assertEqual(smp.absorbance(bg).crop(500, 2500).quantity, "absorbance")


class TestExport(unittest.TestCase):
    spectrum = Spectrum(np.array([1000.0, 2000.0]), np.array([0.5, np.nan]), "absorbance")
    expected = {"wavenumber_cm-1": [1000.0, 2000.0], "absorbance": [0.5, None]}

    def test_df(self):
        self.assertEqual(self.spectrum.to_df().to_dict(as_series=False), self.expected)

    def test_csv(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.csv")
            self.spectrum.to_csv(path)
            with open(path) as f:
                self.assertEqual(f.read(), "wavenumber_cm-1,absorbance\n1000.0,0.5\n2000.0,\n")

    def test_parquet(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.parquet")
            self.spectrum.to_parquet(path)
            self.assertEqual(pl.read_parquet(path).to_dict(as_series=False), self.expected)


if __name__ == "__main__":
    unittest.main()
