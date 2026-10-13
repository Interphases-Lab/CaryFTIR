
"""Health checks: limits, individual checks, and the behaviour through the driver."""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from caryftir import CaryFTIR, HealthMonitor, InstrumentShutdown, Level, Limits
from caryftir.protocol import Tag
from caryftir.transport import Link
from tests.fake_instrument import FakeInstrument


def scan_with(**info):
    return SimpleNamespace(info={getattr(Tag, k.upper()): v for k, v in info.items()}, blocks={})


class TestLimits(unittest.TestCase):
    def test_levels(self):
        limits = Limits(warning_low=38, warning_high=42, error_low=36, error_high=44)
        self.assertEqual([limits.level(v) for v in (35, 37, 40, 43, 45)],
                         [Level.ERROR, Level.WARNING, Level.OK, Level.WARNING, Level.ERROR])
        self.assertEqual(Limits(warning_high=70).level(-1000), Level.OK)  # unset sides are not checked


class TestMonitor(unittest.TestCase):
    def test_block_temperature(self):
        monitor = HealthMonitor()
        self.assertEqual(monitor.check(scan_with(block_temperature=49.25)), [])
        [warning] = monitor.check(scan_with(block_temperature=75.0))
        self.assertEqual((warning.check, warning.level), ("block_temperature", Level.WARNING))
        self.assertIn("above 70", warning.message)
        [error] = monitor.check(scan_with(block_temperature=85.0))
        self.assertEqual(error.level, Level.ERROR)

    def test_missing_sensor_is_ignored(self):
        self.assertEqual(HealthMonitor().check(scan_with(block_temperature=999.0)), [])

    def test_shutdown_flags(self):
        [f] = HealthMonitor().check(scan_with(shutdown_flags=0x01))
        self.assertEqual((f.check, f.level), ("shutdown", Level.ERROR))
        [f] = HealthMonitor().check(scan_with(shutdown_flags=0x10))
        self.assertIn("battery", f.message)

    def test_accessory_change(self):
        monitor = HealthMonitor()
        self.assertEqual(monitor.check(scan_with(sampling_accessory=3)), [])
        [f] = monitor.check(scan_with(sampling_accessory=5))
        self.assertEqual((f.check, f.level), ("accessory", Level.WARNING))

    def test_custom_limits(self):
        monitor = HealthMonitor(limits={"block_temperature": Limits(warning_high=40)})
        self.assertEqual(monitor.check(scan_with(block_temperature=49.25))[0].level, Level.WARNING)


@mock.patch.object(time, "sleep", lambda s: None)
class TestThroughDriver(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInstrument()
        self.ftir = CaryFTIR(Link(self.fake))
        self.ftir.connect()
        self.ftir.start()

    def test_normal_scan_is_healthy(self):
        self.assertEqual(self.ftir.next_scan().health, [])

    def test_overheating_is_reported(self):
        self.fake.status[0x1984] = 82.0
        [f] = self.ftir.next_scan().health
        self.assertEqual((f.check, f.level), ("block_temperature", Level.ERROR))

    def test_low_energy_is_reported(self):
        with mock.patch("caryftir.health.decode_raw") as decode:
            decode.return_value = SimpleNamespace(ir=[0, 4_000_000], laser=[0, 1_500_000])  # 15625, 5859
            [f] = self.ftir.next_scan().health
        self.assertEqual((f.check, f.level), ("energy", Level.ERROR))

    def test_shutdown_raises_after_releasing_the_scan(self):
        self.fake.status[0x1901] = 0x01
        with self.assertRaises(InstrumentShutdown):
            self.ftir.next_scan()
        self.assertFalse(self.fake.scan_pending)


if __name__ == "__main__":
    unittest.main()
