
"""Minimal pcapng + USBPcap parser (no deps)."""
import struct, sys
from dataclasses import dataclass


@dataclass
class Urb:
    idx: int          # 1-based frame number (matches Wireshark)
    ts: float
    irp: int
    status: int
    function: int
    direction_in: bool  # info bit0: 1 = completion (PDO->FDO)
    bus: int
    dev: int
    ep: int           # with 0x80 bit for IN
    xfer: int         # 0 iso,1 int,2 ctrl,3 bulk
    data: bytes


def read(path):
    out = []
    with open(path, 'rb') as f:
        buf = f.read()
    off = 0
    endian = '<'
    ifaces = []
    idx = 0
    while off + 12 <= len(buf):
        btype, blen = struct.unpack_from(endian + 'II', buf, off)
        if btype == 0x0A0D0D0A:
            bom = struct.unpack_from('<I', buf, off + 8)[0]
            endian = '<' if bom == 0x1A2B3C4D else '>'
            blen = struct.unpack_from(endian + 'I', buf, off + 4)[0]
        elif btype == 1:
            lt, = struct.unpack_from(endian + 'H', buf, off + 8)
            ifaces.append({'lt': lt, 'tsres': 1e-6})
        elif btype == 6:
            iid, tsh, tsl, caplen, origlen = struct.unpack_from(endian + 'IIIII', buf, off + 8)
            pkt = buf[off + 28: off + 28 + caplen]
            idx += 1
            ts = ((tsh << 32) | tsl) * 1e-6
            if ifaces[iid]['lt'] == 249 and len(pkt) >= 27:
                hl, irp, st, fn, info, bus, dev, ep, xf, dl = struct.unpack_from('<HQIHBHHBBI', pkt, 0)
                out.append(Urb(idx, ts, irp, st, fn, bool(info & 1), bus, dev, ep, xf, pkt[hl:hl + dl]))
        elif btype == 3:
            idx += 1
        off += blen
    return out


if __name__ == '__main__':
    # usage: usbpcap.py file.pcapng  -> number of transfers per (device, endpoint)
    from collections import Counter
    counts = Counter((u.dev, u.ep) for u in read(sys.argv[1]) if u.data)
    for (dev, ep), n in sorted(counts.items()):
        print(f"device {dev:3d}  EP 0x{ep:02x}: {n}")
