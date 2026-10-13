
"""Choosing an instrument when one or several are connected (no USB hardware needed)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from caryftir import transport
from caryftir.transport import UsbLocation, find_instruments, select_device


def device(bus, address, ports=(2,)):
    return SimpleNamespace(bus=bus, address=address, port_numbers=ports)


class TestSelectDevice(unittest.TestCase):
    def test_single_instrument_is_found_automatically(self):
        only = device(1, 5)
        self.assertIs(select_device([only]), only)

    def test_none_connected(self):
        with self.assertRaisesRegex(IOError, "no instrument connected"):
            select_device([])

    def test_several_need_a_choice(self):
        devices = [device(1, 5), device(2, 9, (1, 3))]
        with self.assertRaisesRegex(IOError, "several instruments connected.*bus 2, address 9 \\(port 2-1.3\\)"):
            select_device(devices)

    def test_choose_by_bus_and_address(self):
        devices = [device(1, 5), device(2, 9), device(2, 5)]
        self.assertIs(select_device(devices, bus=2, address=5), devices[2])
        self.assertIs(select_device(devices, address=9), devices[1])  # unique without the bus

    def test_ambiguous_or_missing_choice(self):
        devices = [device(1, 5), device(2, 5)]
        with self.assertRaisesRegex(IOError, "several instruments match address 5"):
            select_device(devices, address=5)
        with self.assertRaisesRegex(IOError, "no instrument at bus 3, address 1"):
            select_device(devices, bus=3, address=1)

    def test_choose_by_port_path(self):
        devices = [device(1, 5, (2,)), device(1, 9, (2, 3)), device(2, 4, (2, 3))]
        self.assertIs(select_device(devices, port_path="1-2.3"), devices[1])
        self.assertIs(select_device(devices, port_path="2-2.3"), devices[2])  # same hub ports, other bus
        with self.assertRaisesRegex(IOError, "no instrument at port_path 1-4"):
            select_device(devices, port_path="1-4")

    def test_criteria_combine(self):
        devices = [device(1, 5, (2,)), device(1, 9, (3,))]
        self.assertIs(select_device(devices, bus=1, port_path="1-3"), devices[1])
        with self.assertRaisesRegex(IOError, "no instrument at address 5, port_path 1-3"):
            select_device(devices, address=5, port_path="1-3")


class TestFindInstruments(unittest.TestCase):
    def test_locations(self):
        with mock.patch.object(transport, "_find_devices", return_value=[device(1, 5, (2,)), device(3, 7, (1, 4))]):
            self.assertEqual(find_instruments(), [UsbLocation(1, 5, "1-2"), UsbLocation(3, 7, "3-1.4")])


if __name__ == "__main__":
    unittest.main()
