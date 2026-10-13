# CaryFTIR

*Based on the great work of a bachelor thesis project in our group: Z. Albitar, V. Back, H. Backlund, A. Lindberg and B. Sienko, [“Cracking The Code: Reverse-Engineering Proprietary Instrument Protocols for Interoperability Automation”](https://urn.kb.se/resolve?urn=urn:nbn:se:uu:diva-588078), Uppsala University, 2026.*

##

A Python driver for the Agilent Cary 630 FTIR spectrometer over USB. It runs on any OS with libusb, so no vendor software is needed. It connects to the instrument, configures it, downloads scans and turns them into spectra.

The USB protocol is documented in [`docs/protocol.md`](docs/protocol.md). This is a best-effort implementation based on observed USB traffic.

> **Status:** tested against messages from USB captures of the original software and against a simulated instrument. It has **not yet been exhaustively tested against the real instrument**; see [Testing on the instrument](#testing-on-the-instrument).

## Installation

The project uses [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/Interphases-Lab/CaryFTIR
cd CaryFTIR
uv sync                  # creates .venv with numpy, scipy, polars and pyusb
uv run caryftir --list   # should list the connected instrument
```

To use it from another uv project, run `uv add git+https://github.com/Interphases-Lab/CaryFTIR`. Plain pip works too: `pip install git+https://github.com/Interphases-Lab/CaryFTIR`.

pyusb needs libusb and access to the instrument (USB ID `0fb4:0201`):

| OS | Setup |
|----|-------|
| macOS | install libusb, e.g. `brew install libusb`. On Apple Silicon, Python may not find Homebrew's libusb (`usb.core.NoBackendError`); fix with `export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` |
| Linux | add a udev rule for `0fb4:0201` so you don't need root, then re-plug the instrument |
| Windows | bind the instrument to WinUSB with [Zadig](https://zadig.akeo.ie). This replaces the vendor driver, so the original software won't work until you roll the driver back in Device Manager |

## Quick start

```sh
uv run caryftir --list                     # connected instruments
uv run caryftir --scans 5 --out scans.npz  # take 5 scans, save raw data and the averaged single beam
```

Add `-v` for driver debug messages, or `-vv` to also log every USB frame.

## Usage

### Scans

```python
from caryftir import CaryFTIR, InterferogramSettings

with CaryFTIR.open() as ftir:                 # connects; disconnects on exit
    print(ftir.info())                        # serial number, laser wavenumber, gains
    ftir.start(InterferogramSettings(resolution=4))
    for _ in range(10):
        scan = ftir.next_scan()               # waits for the scan, downloads it, releases it
        ir, laser = scan.raw.ir, scan.raw.laser
```

`next_scan()` returns a `Scan` with:

* `raw.ir` / `raw.laser`: interferogram and reference-laser samples
* `info`: the instrument's values for this scan (tag → value), e.g. `info[Tag.RUN_ID]`
* `status`: scan count, run ID and the data types the instrument offered
* `health`: warnings and errors for this scan (see [Instrument health](#instrument-health))

To change settings mid-session, call `start()` again. Scans taken with the new settings carry a new `status.run_id`.

`start()` runs these steps in order, and each is also available on its own:

| Method | Does |
|--------|------|
| `configure()` | sends interferogram settings, spectrum settings and the number of scans to average |
| `request_data()` | chooses which data types the instrument delivers per scan |
| `new_run()` | starts a new run ID |
| `refresh_info()` | asks the instrument to refresh its information and waits until done |

### Spectra

```python
from caryftir import CaryFTIR

with CaryFTIR.open() as ftir:
    ftir.start()
    background = ftir.measure_single_beam(scans=16)   # averages good scans, rejects bad ones
    input("Load the sample, then press Enter")
    sample = ftir.measure_single_beam(scans=16)

absorbance = sample.absorbance(background).crop(650, 4000)   # .wavenumbers, .values
absorbance.to_csv("sample.csv")                               # or .to_parquet(), .to_df()
```

`to_df()` returns a polars DataFrame with two columns: `wavenumber_cm-1` and the spectrum's quantity (`single_beam`, `transmittance` or `absorbance`). `to_csv()` and `to_parquet()` write that DataFrame. Regions without enough light are null: empty fields in CSV.

See [Spectrum processing](#spectrum-processing) for how spectra are computed.

### Several instruments on one PC

`CaryFTIR.open()` finds the instrument automatically when exactly one is connected. With several connected, it raises an error listing them instead of guessing. Choose one by port path or by bus and address:

```python
from caryftir import CaryFTIR, find_instruments

print(find_instruments())   # [UsbLocation(bus=1, address=5, port_path='1-2'), UsbLocation(bus=3, address=7, port_path='3-1.4')]
with CaryFTIR.open(port_path="3-1.4") as ftir:      # or: CaryFTIR.open(bus=3, address=7)
    ...
```

On the command line: `--port 3-1.4`, or `--bus 3 --address 7`.

| Choose by | Stays valid | Use for |
|-----------|-------------|---------|
| `port_path`: bus, then hub ports from the root, e.g. `3-1.4` | while the instrument stays in the same USB socket, including re-plugging and reboots | fixed setups, in scripts or config files |
| `bus` + `address` | until the instrument is re-plugged or the PC restarts | one-off choices; look it up again with `--list` |

A port path identifies the socket, not the instrument. If two instruments swap sockets, each script opens the other one. Bus numbers can also change if the PC's USB controllers are reordered, e.g. after adding a USB card. The criteria can be combined, and all given ones must match.

### Spectra from the instrument (experimental)

Some evidence seems to imply the instrument can calculate single-beam spectra itself. This has not been tested, and is not planned, but is possible to try with `DataType.SINGLE_BEAM`:

```python
from caryftir import DataType

ftir.start(single=DataType.RAW | DataType.SINGLE_BEAM)
scan = ftir.next_scan()
spectrum = scan.blocks[DataType.SINGLE_BEAM].values()   # if the instrument delivers it
```

## Spectrum processing

`caryftir.processing` builds on the processing from the thesis prototype: IR sampled at the laser zero crossings, then a Mertz-phase-corrected FFT. With the prototype's settings it reproduces its output to within 0.1%. The changes below aim at the original software's data quality; the numbers were measured on captures during development.

| Change | Effect |
|--------|--------|
| Scans with a misplaced centerburst, a peak more than 20% off, or a weak laser are rejected | the first scan of every session is such a scan |
| Happ–Genzel apodization (other windows selectable), plus a triangular window on the phase region | Same as default choice in the original software. |
| Cubic instead of linear interpolation at the laser crossings | linear loses about 0.5% intensity at 3800 vs 1000 cm⁻¹; cubic less than 0.02% |
| Exact wavenumber axis on the original software's grid, `k · 2 · 7633.588 / N` (1.8637 cm⁻¹) | the same grid as the original software |
| Laser temperature correction (**educated guess**, on by default) | follows how the original software appears to correct the axis; see "Laser temperature correction" in [`docs/protocol.md`](docs/protocol.md) |
| No smoothing; regions without enough light become NaN | bands aren't blurred, and the grid stays intact |

Settings are in `ProcessingSettings`: apodization, phase points, zero filling, interpolation and temperature correction.

## Instrument health

Every scan is checked, and the results are in `scan.health`:

```python
for finding in scan.health:       # e.g. "ERROR: block temperature 82.0 °C (above 80)"
    print(finding.level.name, finding.check, finding.value)
```

* **Shutdown:** if the instrument signals that it is shutting down, or that its battery is critical, `next_scan()` raises `InstrumentShutdown`.
* **Limit checks:** everything else is a warning or an error, and scanning continues. The checks are:
  * block temperature: the original software's "overheating" error, a warning above 70 °C and an error above 80 °C
  * detector temperature
  * energy and laser signal
  * a change of sampling accessory

The default limits are the original software's (`caryftir.health.DEFAULT_LIMITS`). The full table is in "Status, warnings and errors" in [`docs/protocol.md`](docs/protocol.md). To change them:

```python
from caryftir import CaryFTIR, HealthMonitor, Limits
from caryftir.health import DEFAULT_LIMITS

ftir = CaryFTIR.open()
ftir.health = HealthMonitor(limits={**DEFAULT_LIMITS, "energy": Limits(warning_low=14000, error_low=12000)})
```

Expect energy errors with the defaults. In the captures, energy reads about 15 800, below the default error limit of 20 000. Automatic gain adjustment is not yet implemented. 

## Safety

Command `0xB1` writes the instrument's non-volatile memory, such as calibration data, which a power cycle does not reset. Normal operation never needs it. It is known (`Cmd.WRITE_NONVOLATILE`) but blocked: the transport layer refuses it with `BlockedCommandError`. See "Blocked" in [`docs/protocol.md`](docs/protocol.md).

## Testing on the instrument

Run `uv run caryftir --scans 5 --out scans.npz` and check:

1. All 5 scans arrive.
2. The program exits cleanly, and the original software can connect afterwards without power-cycling.
3. With `--single-beam`: does each scan list block `0x100`? If so, compare that spectrum with the original software's.
4. Data quality: measure a sample with the original software while capturing the USB traffic, and export its spectrum. The capture contains the raw scans, so `caryftir.processing` can be compared directly against the export.
5. Health: compare the temperatures and energy from `caryftir.health.readings(scan)` with the original software's status display. Does it also report low energy?

If something fails, a Wireshark/USBPcap capture of the run is the quickest way to compare with the original software's traffic (see `tools/`).

## Development

```sh
uv run python -m unittest discover -s tests -t .
```

`tests/fake_instrument.py` simulates the instrument at the USB-endpoint level and asserts on the mistakes that hang the real one.

| Path | Content |
|------|---------|
| `caryftir/protocol.py` | constants, message encoding and decoding; no I/O |
| `caryftir/transport.py` | USB device discovery, pyusb endpoints, framing |
| `caryftir/instrument.py` | `CaryFTIR`: connect, start/stop, scan loop |
| `caryftir/data.py` | decoding of downloaded blocks |
| `caryftir/processing.py` | raw scans → averaged single beam → transmittance / absorbance; export to polars, CSV, Parquet |
| `caryftir/health.py` | warnings, errors and the shutdown signal |
| `docs/protocol.md` | the USB protocol |
| `tools/` | decoders for Wireshark/USBPcap captures: `decode_frames.py` (commands and replies), `decode_tlv.py` (value lists) |
