import sqlite3
from dataclasses import dataclass

from intake.ledger import StationLedger
from intake.originals import load_original
from intake.receipts import Receipt, load_receipt
from quality.limits import GradeDecision, classify_peak
from quality.peaks import Peak, find_peak
from shift.opening import load_protocol
from shift.protocol import ShiftProtocol


@dataclass(frozen=True)
class GradeResult:
    receipt: Receipt
    protocol: ShiftProtocol
    peak: Peak
    decision: GradeDecision


def grade_receipt(connection: sqlite3.Connection, protocol: ShiftProtocol,
                  receipt: Receipt) -> GradeResult:
    if receipt.wave_id.shift_id != protocol.shift_id or receipt.wave_id.device_id not in protocol.roster.devices:
        raise RuntimeError('实收首执与判级规程的班次或设备不符')
    packet = load_original(connection, receipt.wave_id)
    if packet is None:
        raise RuntimeError(f'首执 {receipt.receipt_id} 的实收原件缺失')
    peak = find_peak(packet.samples)
    return GradeResult(receipt, protocol, peak, classify_peak(peak, protocol))


def grade(ledger: StationLedger, receipt_id: int) -> GradeResult:
    with ledger.read_transaction() as connection:
        receipt = load_receipt(connection, receipt_id)
        if receipt is None:
            raise LookupError(f'未知首执 {receipt_id!r}')
        protocol = load_protocol(connection, receipt.wave_id.shift_id)
        return grade_receipt(connection, protocol, receipt)
