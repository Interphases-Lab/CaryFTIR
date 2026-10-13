
"""Reassemble EP 0x85 messages and decode the TLV ones (6-byte: tag BE16, value BE32).
usage: decode_tlv.py file.pcapng"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
import usbpcap
urbs = usbpcap.read(sys.argv[1])
dev = next(u.dev for u in urbs if u.ep == 0x85)
msgs = []; cur = b''; need = 0
for u in urbs:
    if u.dev != dev or u.ep != 0x85 or not u.data: continue
    if not cur:
        need = struct.unpack('>I', u.data[4:8])[0] + 8
    cur += u.data
    if len(cur) >= need:
        msgs.append((u.idx, cur[:need])); cur = b''
print('messages', len(msgs))
def tlvs(body):
    return [(struct.unpack('>H', body[i:i+2])[0], struct.unpack('>I', body[i+2:i+6])[0]) for i in range(0, len(body) - 5, 6)]
small = [(i, m) for i, m in msgs if len(m) < 2000]
by_run = {}  # first value list of each (command, run id 0x0031)
for i, m in small:
    t = dict(tlvs(m[8:]))
    by_run.setdefault((m[0], t.get(0x31)), (i, m, t))
for (cmd, run), (i, m, t) in sorted(by_run.items(), key=lambda kv: kv[1][0]):
    print(f"\n# frame {i} cmd={m[0]:02x} hdr={m[:8].hex(' ')} run={run} values={len(t)}")
    print(' '.join(f"{k:04x}={v:x}" for k, v in t.items()))
# which tags change over the session?
allt = [dict(tlvs(m[8:])) for i, m in small if m[0] == 0x64]
keys = sorted(set().union(*allt))
print('\ntags that vary between scans:')
for k in keys:
    vals = [d.get(k) for d in allt]
    if len(set(vals)) > 1:
        uniq = sorted(set(v for v in vals if v is not None))
        print(f"  {k:04x}: {len(uniq)} distinct, e.g. {[hex(x) for x in uniq[:6]]}")
