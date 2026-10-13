# Cary 630 FTIR USB protocol

A best-effort description of the USB protocol of the Agilent Cary 630, as implemented by the driver in `caryftir/`. It is based on Wireshark captures of MicroLab PC. Each part is marked:

* **observed**: seen in the captures;
* **expected**: part of the protocol, but not exercised in the captures;
* **guess**: a meaning inferred from values alone.

To decode a capture:

```
python3 tools/decode_frames.py capture.pcapng 60,61,62,63,64   # commands + replies
python3 tools/decode_tlv.py    capture.pcapng                  # value lists on EP 0x85
```

## Old driver hang

The instrument takes one scan and then waits until the host has fetched and released it (observed: every scan in every capture, without exception):

```
0x60 POLL_SCAN        repeat until status '@' (scan ready)
0x62 SELECT_BLOCK 0x4000 → 0x64 READ_BLOCK → 0x63 RELEASE_BLOCK     (scan info)
0x62 SELECT_BLOCK 0x0001 → 0x64 READ_BLOCK → 0x63 RELEASE_BLOCK     (raw interferogram)
0x61 RELEASE_SCAN
```

The old driver left out the second `0x63` and the `0x61`, so the instrument never started the next scan. It also polled `0x60` a fixed number of times instead of waiting for `'@'`.

## Endpoints (observed)

| EP | Dir | Use |
|----|-----|-----|
| 0x04 | OUT | Commands, always 64-byte packets |
| 0x83 | IN  | Replies, always 64-byte packets |
| 0x06 | OUT | Messages longer than 60 bytes, announced by frame type `0x0A` |
| 0x85 | IN  | Data (value lists, blocks), announced by frame type `0x0B` |

## Framing

Every packet on EP 0x04 and 0x83 has the following layout:

```
[type][seq][len_hi][len_lo] [message, len bytes] [zero padding to 64]
```

| Type | Meaning | |
|------|---------|--|
| `0x08` | command, message ≤ 60 bytes | observed |
| `0x09` | reply | observed |
| `0x0A` | host announces a longer message: body = BE32 length. The device answers with an empty `0x0A`, the message follows on EP 0x06 in ≤512-byte pieces, then a normal `0x09` reply arrives | observed |
| `0x0B` | device announces data on EP 0x85: body = BE32 total length | observed |
| `0x0C` | device rejects the frame | expected |
| `0x0D` | link reset, the first frame of a session. Reply body: 4 bytes (`07 ff 00 01`) | observed |

The sequence byte increments once per message and is echoed in the reply.

## Messages

Every command message starts with `[command][address][0][n]`, followed by fields. All multi-byte fields are **big-endian**. The address byte is 0 except where noted. `n` is non-zero only for `0x15`, where it is 8.

In replies, byte 2 is the status:

| Status | Meaning |
|--------|---------|
| `'@'` 0x40 | OK |
| `'A'` 0x41 | error |
| `'C'` 0x43 | still scanning |
| `'D'` 0x44 | last chunk of a block |
| `'E'` 0x45 | failed |
| `` '`' `` 0x60 | busy |

The original software leaves leftovers from earlier messages in unused message bytes. Those bytes carry no meaning, and much of the "magic" hex in the old driver came from them.

Every message on EP 0x85 starts with an 8-byte header: `[command][index][status][0x10][BE32 payload length]`.

## Commands

| Cmd | Name | Request | Reply | |
|-----|------|---------|-------|--|
| `0x01` | CONNECT | bytes 4–5 = `01 2a` | `'@'` | observed |
| `0x19` | CONNECT_CONFIRM | – | `'@'` | observed |
| `0x68` | GET_DATA_TYPES | – | bytes 4–7 = data types available per scan, 8–11 = available as averages (this instrument: `0x4111`, `0x0110`) | observed |
| `0x18` | READ_VALUE_LIST | address = scan index, bytes 4–7 = list (`0x4000` scan info, `0x2000` system info) | `0x0B` + value list on EP 0x85 | observed |
| `0x69` | REQUEST_DATA_TYPES | bytes 4–7 = data types wanted per scan, 8–11 = averaged types wanted | `'@'` | observed |
| `0xB2` | GET_VALUE | bytes 4–7 = tag (48-byte message) | bytes 8–9 = value length, value from byte 16 | observed |
| `0xB3` | SET_VALUE | bytes 4–7 = tag, 8–9 = length, 12–15 = 0 or 1 (meaning unknown), value from byte 16 | `'@'` | observed |
| `0xB4` | SET_VALUES | bytes 4–5 = size of the value list, 9 = transaction id, value list from byte 16 | `'@'` | observed |
| `0xB6` | REFRESH_INFO | byte 9 = transaction id | `'@'` | observed |
| `0xB5` | POLL_TRANSACTION | byte 9 = transaction id | `'@'` done, `` '`' `` busy | observed |
| `0x15` | CONTROL | byte 4 = control id, 5 = 1, 8–9 = data length, 10–11 = 4, data from byte 16 | `'@'` | observed |
| `0x60` | POLL_SCAN | – | `'C'`, or `'@'` with bytes 4–7 = scans completed, 8–11 = data types in this scan, 12–15 = averaged types, 16–19 = run id | observed |
| `0x62` | SELECT_BLOCK | bytes 4–7 = data type, byte 11 = 1 for the averaged version | bytes 4–7 = element count, 8–11 = element size | observed |
| `0x64` | READ_BLOCK | address = scan index | `0x0B` + one chunk on EP 0x85. Repeat until a chunk has status `'D'` | observed for single chunks; multi-chunk expected |
| `0x63` | RELEASE_BLOCK | – | `'@'` | observed |
| `0x61` | RELEASE_SCAN | – | `'@'` | observed |
| `0x02` | DISCONNECT | – | `'@'` | observed |

* **Transaction id:** one counter shared by `0xB4` and `0xB6`. It starts at 1 and increments per message.
* **Scan index:** starts at 0 after connecting and increments once per scan. The host keeps it, and it matches "scans completed − 1".
* **Other commands:** `0xB0` and `0xB7` appear in the captures (around gain changes); `0xA4`, `0xB8` and `0xB9` are expected. The driver does not use them.

### Blocked: `0xB1` WRITE_NONVOLATILE

`0xB1` writes values the instrument keeps across power cycles, such as calibration data. It is known and deliberately blocked: `protocol.BLOCKED_COMMANDS` lists it, and the transport layer raises `BlockedCommandError`.

For safety reasons the implementation of this has intentionally been deferred. We recommend using the original software for any non-volatile writes and adjustments. 

### Control ids (`0x15`)

| Id | Name | |
|----|------|--|
| `0x10` | START_SCANNING | observed, sent after configuring |
| `0x11` | STOP_SCANNING | observed, sent before disconnecting |
| `0x20` | START_BACKGROUND | expected |
| `0x21` | START_SAMPLE | expected |

The observed stop message also has non-zero bytes 7 and 12–15. These differ from the start message and appear to be don't-care.

## Data types

Data-type ids are used in `0x62` and as bits in the masks of `0x68`, `0x69` and `0x60`.

| Bit | Name | Elements | |
|-----|------|----------|--|
| `0x0001` | RAW | 4 bytes: class byte + 24-bit signed BE value. Even class = IR detector, odd class = reference laser. Trailing `ffffffff` records are unused | observed |
| `0x4000` | SCAN_INFO | value list describing the scan | observed |
| `0x0002` / `0x0004` | RAW_IR / RAW_LASER | BE int32 | expected |
| `0x0010` | INTERFEROGRAM (resampled) | BE int32 | expected |
| `0x0040` | VELOCITY | BE float32 | expected |
| `0x0100` | SINGLE_BEAM | BE float32. Wavenumber range from tags `0x3904`–`0x3914` (guess) | expected |
| `0x0200` | MINI_SINGLE_BEAM | BE float32 | expected |
| `0x0400` | SPECTRUM | BE float32 | expected |

The original software only requests RAW (`0x69` with masks 1, 0) and computes spectra on the PC. However, the instrument reports that it can deliver INTERFEROGRAM and SINGLE_BEAM itself, both per scan and averaged (`0x68` → `0x4111` / `0x0110`). The host also sends spectrum settings (apodization, phase correction). **Requesting SINGLE_BEAM may therefore give finished spectra without host-side processing. This is untested.**

## Values (tags)

Instrument values are addressed by 16-bit tags. The low nibble appears to encode the type: `4` float32, `e` string, otherwise integer (guess, consistent with every observed value). In value lists each value takes 6 bytes: BE16 tag + BE32 value.

| Tag | Name | Observed values | |
|-----|------|-----------------|--|
| `0x0031` | RUN_ID: incremented each time a run starts; scans report it | 1, 2, 3 … | observed |
| `0x0041` | RUN_MODE | 1 | observed |
| `0x0101` | RESOLUTION (cm⁻¹) | 4 | observed |
| `0x0111` | PHASE_POINTS | 512 | observed |
| `0x0121` / `0x0131` | LEFT_PAD / RIGHT_PAD | 128 / 200 | observed |
| `0x0141` | AVERAGE_COUNT: scans co-added | 1, 3, 4, 8 | observed |
| `0x3101` | APODIZATION | 1, 5 | observed |
| `0x3111` | ZERO_FILL | 0 | observed |
| `0x3121` | OFFSET_CORRECTION | 0 | observed |
| `0x3131` | PHASE_CORRECTION | 1 | observed |
| `0x3141` | PHASE_APODIZATION | 1 | observed |
| `0x1041` | SCAN_MODE: 4 = normal | 4 | observed |
| `0x0211` | unknown. Not the scan rate: the time between scans does not follow it. Not used by the driver | 25, 50 | observed |
| `0x0231` | INTERFEROGRAM_PEAK; reset to 0 before starting | e.g. `0x3d5cd8` | observed |
| `0x0221`, `0x0241` | unknown; reset to 0 before starting | 0 | observed |
| `0x102e` | SERIAL_NUMBER | `MY2446CU14` | observed |
| `0x130e`–`0x136e` | version / part strings | | observed |
| `0x1201` / `0x1211` | IR_GAIN / LASER_GAIN | 212 / 204 | observed |
| `0x3154` | MAX_WAVENUMBER | 7630.0 | observed |
| `0x3184` | LASER_WAVENUMBER: reference-laser wavenumber at 0 °C (calibration offset) | 7659.79 | educated guess |
| `0x3194` | LASER_TEMPERATURE_COEFFICIENT: cm⁻¹ per °C (calibration slope) | −0.47 | educated guess |
| `0x31a1` | AXIS_CORRECTION_OFF: 1 disables the temperature correction in the original software | 0 | educated guess |
| `0x31c1` | AXIS_CORRECTION_APPLIED: which correction the original software applied | 2 | educated guess |
| `0x1901` | SHUTDOWN_FLAGS: bit `0x01` instrument shutting down, bit `0x10` battery critical | 0 | expected |
| `0x1911` | SAMPLING_ACCESSORY: changes when the sampling accessory changes | 0 | expected |
| `0x1944` | CPU_TEMPERATURE (°C) | 52.0 | expected |
| `0x1954` | POWER_TEMPERATURE (°C) | 51.9 | expected |
| `0x1964` | IR_TEMPERATURE (°C) | 59.6 | expected |
| `0x1974` | DETECTOR_TEMPERATURE (°C) | 40.0 | expected |
| `0x1984` | BLOCK_TEMPERATURE (°C); also used for the laser temperature correction | 49.25 | expected |
| `0x1934`, `0x1994`, `0x19a4`, `0x19b4` | further temperature slots; 999 = no sensor | 999 | expected |
| `0x19e4` / `0x19f4` | SOURCE_VOLTAGE / SOURCE_CURRENT (IR source) | 2.70 / 1.86 | expected |
| `0xd814` | unknown float; rose from 40.7 to 54.1 within one 75 s capture | | observed |
| `0x3904` / `0x3914` | FIRST/LAST_WAVENUMBER of instrument-computed spectra | | guess |

The resolution sets the interferogram length: at 4 cm⁻¹ a RAW block holds 58 515 records.

### Laser temperature correction (educated guess)

The wavenumber axis depends on the reference laser's wavenumber, which drifts with temperature. The best reading of the values above is:

```
laser wavenumber = 0x3184 + 0x3194 × 0x1984      (7659.79 − 0.47 × 49.25 ≈ 7636.6 cm⁻¹)
```

The original software appears to compute the laser wavenumber like this from the block temperature, then put spectra on a fixed grid of `k · 2 · 7633.588 / N` cm⁻¹. `0x31a1` and `0x31c1` only control and record this correction on the PC. They don't change the measurement, so the driver doesn't write them.

The captured data can't confirm the correction. At 4 cm⁻¹ the apparent peak of the CO₂ Q-branch isn't known to better than a few tenths of a cm⁻¹. In captured backgrounds it lands at 667.79 cm⁻¹ with the correction and 667.52 without it (band centre 667.38). An earlier version used `0x1964` as the temperature, but that is the IR temperature, not the block.

`caryftir.processing` applies the correction by default (`ProcessingSettings.temperature_correction`). It falls back to 7633.588 cm⁻¹ if the values are missing or implausible.

## Status, warnings and errors

The instrument doesn't send error codes for its condition. There are two mechanisms, both visible in every scan:

1. **Shutdown (instrument side).** `0x1901` bit `0x01` means the instrument is shutting down, and bit `0x10` means a critical battery. The original software closes the connection. The driver raises `InstrumentShutdown` from `next_scan()`, after the scan has been released.
2. **Limits (host side).** Everything else, including the original software's "overheating" message, is the PC comparing readings against limits. The default limits come from the original software's configuration file (`MicroLabPC.exe.config` in its installation folder).

Results are a **warning** (the original's "marginal") or an **error** ("critical"). The original software shows them as a status and keeps going.

| Check | Reading | Warning outside | Error outside |
|-------|---------|-----------------|---------------|
| block temperature | `0x1984` | −10 … 70 °C | −20 … 80 °C |
| detector temperature | `0x1974` | 38 … 42 °C | 36 … 44 °C |
| energy | peak \|IR\| of the raw scan ÷ 256 | 22 000 … 28 000 | 20 000 … 30 000 |
| laser | peak \|laser\| of the raw scan ÷ 256 | 5 500 … 15 000 | 3 000 … 17 000 |
| sampling accessory | `0x1911` | any change | |

* **Switched off by default:** the configuration also holds limits for the CPU (43 … 44 / 42 … 45 °C), IR and power temperatures (18 … 47 / 15 … 50 °C), but the original software has them switched off. Captured IR is 59.6 °C.
* **Not configured:** the original software also has fields for source voltage and current limits, but none are set in the configuration.
* **Energy:** the original software aims for an energy of 26 000 by adjusting the IR gain (`0x1201`). The driver doesn't adjust the gain. With the gain in the captures, energy reads about 15 800, so every scan gets an energy error with the default limits. Lower the limit or adjust the gain if that's expected.
* **Confidence:** the meanings of the status tags come from how the original software labels and checks them (marked "expected" in the tag table). The readings and their values are observed.

## A session

1. **Connect:** `0x0D` reset, `0x01`, `0x19`, `0x68`, `0x18` scan info, `0x18` system info.
2. **Start:**
   1. `0xB3` SCAN_MODE = 4
   2. settings (`0xB4` with the ten values above, 76 bytes, so it goes via EP 0x06)
   3. `0xB3` peak and unknown values = 0
   4. `0x69` (requested data types)
   5. `0xB4` new run id
   6. `0x15` START_SCANNING
   7. `0xB6`, then `0xB5` until done

   The original software additionally reads many values and writes `0x0211` (purpose unknown), `0x31a1` and `0x31c1` (temperature correction flags, see above). The driver skips these; none of them changes the measured data.
3. **Scan loop:** see "Why the old driver hung".
4. **New settings mid-session:** send the settings again plus a new run id. Scans then report the new run id.
5. **Stop:** `0x15` STOP_SCANNING, then `0x02`.

## Open questions

* What do the other class-byte bits in RAW records mean? Values 0, 1, 4, 8, 9, 16, 17 … occur.
* What do the `0xB3` flag (bytes 12–15) and tags `0x0211`, `0x0221` and `0x0241` do?
* Does the laser temperature correction hold? 
* The energy limits: with the gain used in the captures, energy is about 15 800, below the default error limit of 20 000 (see "Status, warnings and errors").
* Lots of work to confirm we get the same data as the original software.  