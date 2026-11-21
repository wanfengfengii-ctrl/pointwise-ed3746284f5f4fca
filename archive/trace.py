import sqlite3
from dataclasses import dataclass

from intake.originals import load_original
from intake.receipts import Receipt, load_receipt
from quality.report import ReportEntry
from recorder.packet import WavePacket


@dataclass(frozen=True)
class OriginalTrace:
    entry: ReportEntry
    receipt: Receipt
    packet: WavePacket


def resolve_trace(connection: sqlite3.Connection, entry: ReportEntry) -> OriginalTrace:
    receipt = load_receipt(connection, entry.receipt_id)
    if receipt is None:
        raise RuntimeError(f'历史条目引用的首执 {entry.receipt_id} 缺失')
    if receipt.wave_id != entry.wave_id:
        raise RuntimeError(f'历史条目与首执 {entry.receipt_id} 的原件身份不符')
    packet = load_original(connection, entry.wave_id)
    if packet is None:
        raise RuntimeError(f'历史首执 {entry.receipt_id} 的原件缺失')
    return OriginalTrace(entry, receipt, packet)
