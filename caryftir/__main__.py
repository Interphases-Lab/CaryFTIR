
"""
Quick hardware check:  caryftir --scans 5 --out scans.npz [--single-beam] [-v | -vv]
(or: python -m caryftir ...)

Connects, logs instrument info, takes N scans and saves the IR and laser streams, their
averaged single-beam spectrum (and the instrument's own single beams with --single-beam)
to an .npz file. Health warnings and errors are logged by the driver as they appear.
"""
import argparse
import logging

import numpy as np

from . import CaryFTIR, DataType, InterferogramSettings, SingleBeamAverager, Tag, find_instruments

log = logging.getLogger("caryftir.cli")


def main() -> None:
    parser = argparse.ArgumentParser(prog="caryftir",
                                     description="Quick hardware check of a Cary 630 over USB.")
    parser.add_argument("--scans", type=int, default=3)
    parser.add_argument("--average", type=int, default=1, help="scans co-added by the instrument")
    parser.add_argument("--resolution", type=int, default=4, choices=(1, 2, 4, 8, 16))
    parser.add_argument("--single-beam", action="store_true",
                        help="also request single-beam spectra computed by the instrument (experimental)")
    parser.add_argument("--out", help="save scans to this .npz file")
    parser.add_argument("--list", action="store_true", help="list connected instruments and exit")
    parser.add_argument("--bus", type=int, help="USB bus of the instrument (with several connected)")
    parser.add_argument("--address", type=int, help="USB address of the instrument (with several connected)")
    parser.add_argument("--port", help="USB port path of the instrument, e.g. 1-2.3 (see --list)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v: driver debug messages, -vv: also every USB frame")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.verbose < 2:
        logging.getLogger("caryftir.transport").setLevel(logging.INFO)

    if args.list:
        instruments = find_instruments()
        log.info("%d instrument(s) connected", len(instruments))
        for location in instruments:
            log.info("  %s", location)
        return

    single = DataType.RAW | (DataType.SINGLE_BEAM if args.single_beam else 0)
    arrays = {}
    averager = SingleBeamAverager()
    with CaryFTIR.open(bus=args.bus, address=args.address, port_path=args.port) as ftir:
        log.info("instrument: %s", ftir.info())
        ftir.start(InterferogramSettings(resolution=args.resolution), average_count=args.average, single=single)
        for i in range(args.scans):
            scan = ftir.next_scan()
            raw = scan.raw
            log.info("scan %d: run %d, %d IR samples, IR peak %d, blocks %s", scan.index, scan.status.run_id,
                     len(raw.ir), np.abs(raw.ir).max(), [hex(t) for t in scan.blocks])
            arrays[f"ir_{i}"], arrays[f"laser_{i}"] = raw.ir, raw.laser
            averager.add_raw(raw, scan.info)  # logs the reason if a scan is rejected
            if DataType.SINGLE_BEAM in scan.blocks:
                arrays[f"single_beam_{i}"] = scan.blocks[DataType.SINGLE_BEAM].values()
                arrays[f"single_beam_range_{i}"] = np.array([scan.info.get(Tag.FIRST_WAVENUMBER, np.nan),
                                                             scan.info.get(Tag.LAST_WAVENUMBER, np.nan)])
                log.info("single beam from instrument: %d points", len(arrays[f"single_beam_{i}"]))
    if averager.count:
        spectrum = averager.result()
        arrays["wavenumbers"], arrays["single_beam"] = spectrum.wavenumbers, spectrum.values
        log.info("averaged single beam from %d scans (%d rejected), laser wavenumber %.2f cm-1",
                 averager.count, averager.rejected, np.mean(averager.laser_wavenumbers))
    if args.out:
        np.savez(args.out, **arrays)
        log.info("saved %s", args.out)


if __name__ == "__main__":
    main()
