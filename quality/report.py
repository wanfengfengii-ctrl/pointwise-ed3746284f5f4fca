import sqlite3
from dataclasses import dataclass

from intake.receipts import list_shift_receipts
from quality.grading import grade_receipt
from quality.limits import GradeDecision
from quality.peaks import Peak
from quality.reconcile import MissingRange, reconcile_device
from recorder.endmark import EndMark
from recorder.packet import WaveId
from shift.closing import require_stopped
from shift.protocol import ShiftProtocol


@dataclass(frozen=True)
class ReportEntry:
    receipt_id: int
    wave_id: WaveId
    peak: Peak
    decision: GradeDecision


@dataclass(frozen=True)
class DeviceReport:
    endmark: EndMark
    entries: tuple[ReportEntry, ...]
    missing: tuple[MissingRange, ...]

    @property
    def rechecks(self) -> tuple[ReportEntry, ...]:
        return tuple(entry for entry in self.entries if entry.decision == GradeDecision.RECHECK)


@dataclass(frozen=True)
class ShiftReportDraft:
    shift_id: str
    channel: str
    unit: str
    threshold: int
    devices: tuple[DeviceReport, DeviceReport]


def build_report(connection: sqlite3.Connection, protocol: ShiftProtocol, endmarks) -> ShiftReportDraft:
    marks = require_stopped(protocol, endmarks)
    by_device = {device: [] for device in protocol.roster.devices}
    for receipt in list_shift_receipts(connection, protocol.shift_id):
        if receipt.wave_id.device_id not in by_device:
            raise RuntimeError('当班实收中存在清册外设备')
        by_device[receipt.wave_id.device_id].append(receipt)

    device_reports = []
    for mark in marks:
        coverage = reconcile_device(mark, by_device[mark.device_id])
        entries = []
        for receipt in coverage.receipts:
            result = grade_receipt(connection, protocol, receipt)
            entries.append(ReportEntry(receipt.receipt_id, receipt.wave_id, result.peak, result.decision))
        device_reports.append(DeviceReport(mark, tuple(entries), coverage.missing))
    left, right = device_reports
    return ShiftReportDraft(protocol.shift_id, protocol.channel, protocol.unit, protocol.threshold, (left, right))
