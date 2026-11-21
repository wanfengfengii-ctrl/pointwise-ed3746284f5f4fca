from dataclasses import dataclass

from archive.catalog import ArchiveRecord, load_report
from archive.trace import OriginalTrace, resolve_trace
from intake.ledger import StationLedger


@dataclass(frozen=True)
class ReportReadback:
    record: ArchiveRecord
    traces: tuple[OriginalTrace, ...]


def retrieve(ledger: StationLedger, report_id: int) -> ReportReadback | None:
    with ledger.read_transaction() as connection:
        record = load_report(connection, report_id)
        if record is None:
            return None
        traces = tuple(
            resolve_trace(connection, entry)
            for device in record.report.devices
            for entry in device.entries
        )
        return ReportReadback(record, traces)
