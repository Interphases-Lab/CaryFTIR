
"""Decoding of blocks downloaded from the instrument."""
from dataclasses import dataclass, field

import numpy as np

from .protocol import FLOAT_TYPES, DataType


@dataclass
class Block:
    type: int
    element_size: int  # bytes per element, from the SELECT_BLOCK reply
    payload: bytes = field(repr=False)
    averaged: bool = False

    def values(self) -> np.ndarray:
        """Elements as numbers: raw records, BE float32 for spectra, BE int32 otherwise."""
        if self.type == DataType.RAW:
            return decode_raw(self.payload).values
        if self.element_size == 8:
            return np.frombuffer(self.payload, ">f8").astype(float)
        if self.element_size == 4:
            dtype = ">f4" if self.type & FLOAT_TYPES else ">i4"
            return np.frombuffer(self.payload, dtype).astype(float)
        return np.frombuffer(self.payload, np.uint8)


@dataclass
class RawData:
    """Interleaved records of a RAW block: one class byte + a 24-bit signed BE value."""
    classes: np.ndarray
    values: np.ndarray

    @property
    def ir(self) -> np.ndarray:
        """IR detector samples (records with an even class byte)."""
        return self.values[self.classes % 2 == 0]

    @property
    def laser(self) -> np.ndarray:
        """Reference laser samples (records with an odd class byte)."""
        return self.values[self.classes % 2 == 1]


def decode_raw(payload: bytes) -> RawData:
    records = np.frombuffer(payload[:len(payload) // 4 * 4], np.uint8).reshape(-1, 4)
    # Trailing 0xFFFFFFFF records are unused buffer space.
    used = len(records)
    while used and (records[used - 1] == 0xFF).all():
        used -= 1
    records = records[:used]
    values = (records[:, 1].astype(np.int32) << 16) | (records[:, 2].astype(np.int32) << 8) | records[:, 3]
    values = np.where(values & 0x800000, values - 0x1000000, values)
    return RawData(records[:, 0].copy(), values)
