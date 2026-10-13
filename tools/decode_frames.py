
"""Print frames decoded with the corrected layout:
   transport: [type, seq, len BE16]  + app message of `len` bytes
   app:       [cmd, addr, status, b3, then big-endian fields]
usage: decode_frames.py file.pcapng [cmd,cmd,...] [ep6]"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
import usbpcap

urbs = usbpcap.read(sys.argv[1])
want = None
if len(sys.argv) > 2 and not sys.argv[2].startswith('--'):
    want = {int(x, 16) for x in sys.argv[2].split(',')}
dev = next(u.dev for u in urbs if u.ep in (0x85, 0x83))
t0 = None
last_cmd = None
for u in urbs:
    if u.dev != dev or u.xfer != 3 or not u.data:
        continue
    if t0 is None:
        t0 = u.ts
    d = u.data
    if u.ep in (0x04, 0x83):
        typ, seq, ln = d[0], d[1], struct.unpack('>H', d[2:4])[0]
        app = d[4:4 + ln]
        cmd = app[0] if app else None
        if u.ep == 0x04:
            last_cmd = cmd if typ == 0x08 else ('t%02x' % typ)
        c = cmd if u.ep == 0x04 else last_cmd
        if want is not None and c not in want:
            continue
        print(f"{u.idx:7d} {u.ts - t0:8.3f} {'OUT' if u.ep == 4 else ' IN'} t={typ:02x} seq={seq:02x} len={ln:3d} | {app.hex(' ')}")
    elif u.ep == 0x06:
        if want is None or 'ep6' in sys.argv or (d[0] in want):
            print(f"{u.idx:7d} {u.ts - t0:8.3f} OUT6 {len(d)}B | {d.hex(' ')}")
