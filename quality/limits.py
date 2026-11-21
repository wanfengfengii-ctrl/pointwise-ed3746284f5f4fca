from enum import StrEnum

from quality.peaks import Peak
from shift.protocol import ShiftProtocol


class GradeDecision(StrEnum):
    PASS = 'pass'
    RECHECK = 'recheck'


def classify_peak(peak: Peak, protocol: ShiftProtocol) -> GradeDecision:
    if peak.value > protocol.threshold:
        return GradeDecision.RECHECK
    return GradeDecision.PASS
