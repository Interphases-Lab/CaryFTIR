
"""Encoders against messages copied from Wireshark captures of MicroLab PC."""
import unittest

from caryftir import protocol as P
from caryftir.protocol import Cmd, Control, FrameType, Tag


def h(text: str) -> bytes:
    return bytes.fromhex(text)


class TestFraming(unittest.TestCase):
    def test_command_frame(self):
        # observed: 08 01 00 10 | 01 00 00 00 01 2a ...
        raw = P.encode_frame(FrameType.COMMAND, 1, P.connect_msg())
        self.assertEqual(len(raw), 64)
        self.assertEqual(raw[:10], h("08 01 00 10 01 00 00 00 01 2a"))

    def test_out_data_announcement(self):
        # type 0x0a announcing the 76-byte message on EP 0x06
        self.assertEqual(P.encode_frame(FrameType.OUT_DATA, 0x1F, h("0000004c"))[:8], h("0a 1f 00 04 00 00 00 4c"))

    def test_decode_reply(self):
        frame = P.decode_frame(h("09 25 00 10 69 00 40 00").ljust(64, b"\0"))
        self.assertEqual((frame.type, frame.seq, frame.cmd, frame.status, len(frame.body)),
                         (FrameType.REPLY, 0x25, 0x69, P.Status.OK, 16))


class TestMessages(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(P.connect_msg(), h("01 00 00 00 01 2a 00 00 00 00 00 00 00 00 00 00"))
        self.assertEqual(P.request_data_types_msg(1, 0), h("69 00 00 00 00 00 00 01 00 00 00 00 00 00 00 00"))
        self.assertEqual(P.read_value_list_msg(0x4000), h("18 00 00 00 00 00 40 00").ljust(16, b"\0"))
        self.assertEqual(P.select_block_msg(0x4000), h("62 00 00 00 00 00 40 00").ljust(16, b"\0"))
        self.assertEqual(P.select_block_msg(1), h("62 00 00 00 00 00 00 01").ljust(16, b"\0"))
        self.assertEqual(P.read_block_msg(0x0B), h("64 0b").ljust(16, b"\0"))
        self.assertEqual(P.transaction_msg(Cmd.REFRESH_INFO, 4), h("b6 00 00 00 00 00 00 00 00 04").ljust(16, b"\0"))

    def test_get_value(self):
        msg = P.get_value_msg(0x7019)
        self.assertEqual((len(msg), msg[:8]), (48, h("b2 00 00 00 00 00 70 19")))

    def test_set_value(self):
        # SCAN_MODE (0x1041) = 4 with flag 1
        msg = P.set_value_msg(Tag.SCAN_MODE, P.encode_value(Tag.SCAN_MODE, 4), 1)
        self.assertEqual((len(msg), msg[:20]), (48, h("b3 00 00 00 00 00 10 41 00 04 00 00 00 00 00 01 00 00 00 04")))

    def test_set_values_inline(self):
        # new run id 2, transaction 3
        tlvs = P.encode_tlvs({Tag.RUN_ID: 2, Tag.RUN_MODE: 1})
        self.assertEqual(P.set_values_msg(tlvs, 3),
                         h("b4 00 00 00 00 0c 00 00 00 03 00 00 00 00 00 00 00 31 00 00 00 02 00 41 00 00 00 01"))

    def test_settings_message(self):
        # the 76-byte settings message on EP 0x06 (average count 4, transaction 2)
        tlvs = P.encode_tlvs({0x101: 4, 0x111: 512, 0x121: 128, 0x131: 200, 0x141: 4,
                              0x3101: 1, 0x3111: 0, 0x3121: 0, 0x3131: 1, 0x3141: 1})
        self.assertEqual(P.set_values_msg(tlvs, 2), h(
            "b4 00 00 00 00 3c 00 00 00 02 00 00 00 00 00 00 01 01 00 00 00 04 01 11 00 00 02 00 01 21 00 00"
            " 00 80 01 31 00 00 00 c8 01 41 00 00 00 04 31 01 00 00 00 01 31 11 00 00 00 00 31 21 00 00 00 00"
            " 31 31 00 00 00 01 31 41 00 00 00 01"))

    def test_control_messages(self):
        # Bytes 7 and 12-15 of the observed stop message are treated as don't-care.
        start = P.control_msg(Control.START_SCANNING, bytes(4))
        self.assertEqual(start, h("15 00 00 08 10 01 00 00 00 04 00 04 00 00 00 00 00 00 00 00").ljust(36, b"\0"))
        stop = P.control_msg(Control.STOP_SCANNING, bytes(4))
        observed = h("15 00 00 08 11 01 00 15 00 04 00 04 00 01 00 00 00 00 00 00")
        self.assertEqual(len(stop), 36)
        self.assertEqual([b for i, b in enumerate(stop[:20]) if i != 7 and not 12 <= i < 16],
                         [b for i, b in enumerate(observed) if i != 7 and not 12 <= i < 16])


class TestReplies(unittest.TestCase):
    def test_scan_status(self):
        # "scan ready" reply to POLL_SCAN
        body = h("60 00 40 10 00 00 00 0d 00 00 40 01 00 00 00 00 00 00 00 02 00 00 00 00 00 04 00 00")
        status = P.ScanStatus.from_reply(P.Frame(FrameType.REPLY, 0xF0, body.ljust(48, b"\0")))
        self.assertEqual((status.count, status.single_types, status.averaged_types, status.run_id), (13, 0x4001, 0, 2))

    def test_chunk(self):
        chunk = P.Chunk.decode(h("64 0b 44 10 00 00 00 02 ab cd"))
        self.assertEqual((chunk.cmd, chunk.index, chunk.status, chunk.payload), (0x64, 0x0B, P.Status.DONE, h("ab cd")))
        with self.assertRaises(P.ProtocolError):
            P.Chunk.decode(h("64 0b 44 10 00 00 00 05 ab cd"))

    def test_values(self):
        self.assertAlmostEqual(P.decode_value(0x3184, h("45 ef 5e 52")), 7659.79, places=2)
        self.assertEqual(P.decode_value(0x102E, b"MY2446CU14\0"), "MY2446CU14")
        self.assertEqual(P.decode_value(0x1201, h("00 00 00 d4")), 212)
        self.assertEqual(P.decode_tlvs(h("00 31 00 00 00 05 32 31 00 3d bd 0b")), {0x31: 5, 0x3231: 0x3DBD0B})


if __name__ == "__main__":
    unittest.main()
