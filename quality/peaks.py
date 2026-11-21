from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Peak:
    value: int
    index: int


def find_peak(samples: Sequence[int]) -> Peak:
    if not samples:
        raise ValueError('空波形没有峰值')
    peak = Peak(-1, 0)
    for index, sample in enumerate(samples):
        if type(sample) is not int:
            raise ValueError('峰值计算需要整数微应变样点')
        magnitude = abs(sample)
        if magnitude > peak.value:
            peak = Peak(magnitude, index)
    return peak
