
"""The driver against the simulated instrument in fake_instrument.py."""
import time
import unittest
from unittest import mock

import numpy as np

from caryftir import CaryFTIR, DataType, Tag, decode_raw
from caryftir.protocol import Cmd, message
from caryftir.transport import BlockedCommandError, Link
from tests.fake_instrument import LINES, RAW_RECORDS, FakeInstrument


def connected(fake: FakeInstrument) -> CaryFTIR:
    ftir = CaryFTIR(Link(fake))
    ftir.connect()
    return ftir


@mock.patch.object(time, "sleep", lambda s: None)
class TestInstrument(unittest.TestCase):
    def test_connect(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        self.assertEqual([m[0] for m in fake.sent], [0x01, 0x19, 0x68, 0x18, 0x18])
        self.assertEqual((ftir.available_single, ftir.available_averaged), (0x4111, 0x0110))
        self.assertEqual(ftir.info()["serial_number"], "MY2446CU14")

    def test_start_sequence(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        fake.sent.clear()
        self.assertEqual(ftir.start(average_count=4), 1)
        # Same order as the original software, minus its informational reads.
        self.assertEqual([m[0] for m in fake.sent],
                         [0xB3, 0xB4, 0xB3, 0xB3, 0xB3, 0x69, 0xB4, 0x15, 0xB6, 0xB5, 0xB5, 0xB5])
        self.assertEqual(len(fake.sent[1]), 76)  # settings go via EP 0x06
        self.assertEqual(fake.values[Tag.AVERAGE_COUNT], 4)

    def test_consecutive_scans(self):
        """The old driver hung on the second scan; the simulator asserts on the same mistakes."""
        fake = FakeInstrument()
        ftir = connected(fake)
        ftir.start()
        for i in range(5):
            scan = ftir.next_scan()
            self.assertEqual((scan.index, scan.status.count, scan.info[Tag.RUN_ID]), (i, i + 1, 1))
            raw = scan.raw
            self.assertEqual((len(raw.ir), len(raw.laser)), (RAW_RECORDS // 2 + 1, RAW_RECORDS // 2))
        self.assertFalse(fake.scan_pending)

    def test_multi_chunk_download(self):
        """Blocks larger than one chunk arrive in pieces until a chunk is marked 'D'."""
        fake = FakeInstrument(chunk_size=10000)
        ftir = connected(fake)
        ftir.start()
        scan = ftir.next_scan()
        self.assertEqual(len(scan.blocks[DataType.RAW].payload), RAW_RECORDS * 4)
        reads = [m for m in fake.sent if m[0] == Cmd.READ_BLOCK]
        self.assertEqual(len(reads), 1 + -(-RAW_RECORDS * 4 // 10000))  # scan info + chunks

    def test_measure_single_beam_end_to_end(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        ftir.start()
        spectrum = ftir.measure_single_beam(scans=3)
        for line in LINES:
            near = np.abs(spectrum.wavenumbers - line) < 30
            self.assertLess(abs(spectrum.wavenumbers[near][np.argmax(spectrum.values[near])] - line), 1.0)

    def test_instrument_spectrum(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        ftir.start(single=DataType.RAW | DataType.SINGLE_BEAM)
        scan = ftir.next_scan()
        spectrum = scan.blocks[DataType.SINGLE_BEAM].values()
        np.testing.assert_allclose(spectrum[:3], [0.0, 0.1, 0.2], rtol=1e-6)

    def test_disconnect_stops_scanning(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        ftir.start()
        ftir.next_scan()
        fake.sent.clear()
        ftir.disconnect()
        self.assertEqual([(m[0], m[4]) for m in fake.sent], [(0x15, 0x11), (0x02, 0)])
        self.assertFalse(fake.scanning)


class TestBlockedCommands(unittest.TestCase):
    def test_nonvolatile_write_never_reaches_usb(self):
        fake = FakeInstrument()
        ftir = connected(fake)
        sent_before = len(fake.sent)
        with self.assertRaises(BlockedCommandError):
            ftir.link.request(message(Cmd.WRITE_NONVOLATILE, 0x1234))
        self.assertEqual(len(fake.sent), sent_before)


class TestRawDecoding(unittest.TestCase):
    def test_sign_and_trailing_padding(self):
        payload = bytes.fromhex("02 00 00 05  03 ff ff fb  02 80 00 00  ff ff ff ff  ff ff ff ff")
        raw = decode_raw(payload)
        self.assertEqual(raw.ir.tolist(), [5, -0x800000])
        self.assertEqual(raw.laser.tolist(), [-5])


if __name__ == "__main__":
    unittest.main()
