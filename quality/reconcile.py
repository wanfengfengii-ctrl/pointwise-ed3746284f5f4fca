from collections.abc import Iterable
from dataclasses import dataclass

from intake.receipts import Receipt
from recorder.endmark import EndMark


@dataclass(frozen=True)
class MissingRange:
    first: int
    last: int

    def __post_init__(self):
        if type(self.first) is not int or type(self.last) is not int or not 1 <= self.first <= self.last:
            raise ValueError('未实收闭区间无效')


@dataclass(frozen=True)
class DeviceCoverage:
    endmark: EndMark
    receipts: tuple[Receipt, ...]
    missing: tuple[MissingRange, ...]


def reconcile_device(endmark: EndMark, receipts: Iterable[Receipt]) -> DeviceCoverage:
    ordered = tuple(sorted(receipts, key=lambda receipt: receipt.wave_id.sequence))
    missing = []
    next_expected = 1
    for receipt in ordered:
        identity = receipt.wave_id
        if (identity.shift_id, identity.device_id) != (endmark.shift_id, endmark.device_id):
            raise RuntimeError('实收首执与对账设备或班次不符')
        sequence = identity.sequence
        if not next_expected <= sequence <= endmark.last_sequence:
            raise RuntimeError('对账实收源序重复或超出设备末序')
        if sequence > next_expected:
            missing.append(MissingRange(next_expected, sequence - 1))
        next_expected = sequence + 1
    if next_expected <= endmark.last_sequence:
        missing.append(MissingRange(next_expected, endmark.last_sequence))
    return DeviceCoverage(endmark, ordered, tuple(missing))
