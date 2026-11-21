from collections.abc import Callable, Mapping

from archive.catalog import ArchiveRecord, find_shift_report, publish_report
from intake.ledger import StationLedger
from quality.report import build_report
from recorder.endmark import load_endmark
from recorder.journal import DeviceJournal
from shift.closing import require_stopped
from shift.opening import load_protocol


def seal(ledger: StationLedger, sources: Mapping[str, DeviceJournal], shift_id: str, endmarks, *,
         before_commit: Callable[[ArchiveRecord], None] | None = None) -> ArchiveRecord:
    with ledger.read_transaction() as station:
        protocol = load_protocol(station, shift_id)
    marks = require_stopped(protocol, endmarks)
    for mark in marks:
        journal = sources.get(mark.device_id)
        if journal is None or journal.device_id != mark.device_id:
            raise ValueError(f'无法核验设备 {mark.device_id!r} 的末序来源')
        if load_endmark(journal, shift_id) != mark:
            raise ValueError(f'设备 {mark.device_id!r} 的输入末序与源库不符')

    with ledger.write_transaction() as station:
        previous = find_shift_report(station, shift_id)
        if previous is not None:
            if tuple(device.endmark for device in previous.report.devices) != marks:
                raise RuntimeError('已封班报的末序与持久源末序不符')
            return previous
        record = publish_report(station, build_report(station, protocol, marks))
        if before_commit is not None:
            before_commit(record)
        return record
