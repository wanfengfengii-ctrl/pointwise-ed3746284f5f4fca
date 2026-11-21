import json
import sqlite3
from dataclasses import dataclass

from quality.limits import GradeDecision
from quality.peaks import Peak
from quality.reconcile import MissingRange
from quality.report import DeviceReport, ReportEntry, ShiftReportDraft
from recorder.endmark import EndMark
from recorder.packet import WaveId


@dataclass(frozen=True)
class ArchiveRecord:
    report_id: int
    report: ShiftReportDraft


def initialize_archive_schema(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS archive_reports (
            report_id INTEGER PRIMARY KEY,
            shift_id TEXT NOT NULL UNIQUE REFERENCES shifts (shift_id),
            snapshot TEXT NOT NULL
        )
    ''')


def _encode_report(report: ShiftReportDraft) -> str:
    devices = []
    for device in report.devices:
        entries = []
        for entry in device.entries:
            entries.append({
                'receipt_id': entry.receipt_id,
                'sequence': entry.wave_id.sequence,
                'peak_value': hex(entry.peak.value),
                'peak_index': entry.peak.index,
                'decision': entry.decision.value,
            })
        devices.append({
            'device_id': device.endmark.device_id,
            'last_sequence': device.endmark.last_sequence,
            'entries': entries,
            'missing': [[gap.first, gap.last] for gap in device.missing],
        })
    return json.dumps({
        'shift_id': report.shift_id,
        'channel': report.channel,
        'unit': report.unit,
        'threshold': hex(report.threshold),
        'devices': devices,
    }, separators=(',', ':'))


def _decode_device(shift_id: str, data) -> DeviceReport:
    mark = EndMark(shift_id, data['device_id'], data['last_sequence'])
    entries = []
    for item in data['entries']:
        identity = WaveId(shift_id, mark.device_id, item['sequence'])
        peak = Peak(int(item['peak_value'], 16), item['peak_index'])
        entries.append(ReportEntry(item['receipt_id'], identity, peak, GradeDecision(item['decision'])))
    return DeviceReport(mark, tuple(entries), tuple(MissingRange(*gap) for gap in data['missing']))


def _read_record(row: sqlite3.Row | None) -> ArchiveRecord | None:
    if row is None:
        return None
    report_id = row['report_id']
    try:
        data = json.loads(row['snapshot'])
        shift_id = data['shift_id']
        if shift_id != row['shift_id']:
            raise ValueError('快照班次与索引不符')
        left, right = (_decode_device(shift_id, device) for device in data['devices'])
        if left.endmark.device_id == right.endmark.device_id:
            raise ValueError('班报含重复设备')
        report = ShiftReportDraft(shift_id, data['channel'], data['unit'], int(data['threshold'], 16), (left, right))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'报号 {report_id} 的快照无效') from error
    return ArchiveRecord(report_id, report)


def find_shift_report(connection: sqlite3.Connection, shift_id: str) -> ArchiveRecord | None:
    return _read_record(connection.execute(
        'SELECT * FROM archive_reports WHERE shift_id = ?', (shift_id,),
    ).fetchone())


def publish_report(connection: sqlite3.Connection, report: ShiftReportDraft) -> ArchiveRecord:
    cursor = connection.execute('''
        INSERT INTO archive_reports (shift_id, snapshot) VALUES (?, ?)
    ''', (report.shift_id, _encode_report(report)))
    return ArchiveRecord(cursor.lastrowid, report)


def load_report(connection: sqlite3.Connection, report_id: int) -> ArchiveRecord | None:
    if type(report_id) is not int or not 1 <= report_id <= (1 << 63) - 1:
        return None
    return _read_record(connection.execute(
        'SELECT * FROM archive_reports WHERE report_id = ?', (report_id,),
    ).fetchone())
