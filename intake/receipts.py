import sqlite3
from dataclasses import dataclass

from recorder.packet import WaveId


@dataclass(frozen=True)
class Receipt:
    receipt_id: int
    request_id: str
    wave_id: WaveId


def _receipt_from_row(row: sqlite3.Row | None) -> Receipt | None:
    if row is None:
        return None
    return Receipt(
        row['receipt_id'], row['request_id'],
        WaveId(row['shift_id'], row['device_id'], row['sequence']),
    )


def issue_receipt(connection: sqlite3.Connection, request_id: str, wave_id: WaveId) -> Receipt:
    cursor = connection.execute('''
        INSERT INTO receipts (request_id, shift_id, device_id, sequence) VALUES (?, ?, ?, ?)
    ''', (request_id, wave_id.shift_id, wave_id.device_id, wave_id.sequence))
    return Receipt(cursor.lastrowid, request_id, wave_id)


def find_request_receipt(connection: sqlite3.Connection, request_id: str) -> Receipt | None:
    return _receipt_from_row(connection.execute(
        'SELECT * FROM receipts WHERE request_id = ?', (request_id,),
    ).fetchone())


def find_wave_receipt(connection: sqlite3.Connection, wave_id: WaveId) -> Receipt | None:
    return _receipt_from_row(connection.execute('''
        SELECT * FROM receipts WHERE shift_id = ? AND device_id = ? AND sequence = ?
    ''', (wave_id.shift_id, wave_id.device_id, wave_id.sequence)).fetchone())


def load_receipt(connection: sqlite3.Connection, receipt_id: int) -> Receipt | None:
    if type(receipt_id) is not int or not 1 <= receipt_id <= (1 << 63) - 1:
        return None
    return _receipt_from_row(connection.execute(
        'SELECT * FROM receipts WHERE receipt_id = ?', (receipt_id,),
    ).fetchone())


def list_shift_receipts(connection: sqlite3.Connection, shift_id: str) -> tuple[Receipt, ...]:
    rows = connection.execute('''
        SELECT * FROM receipts WHERE shift_id = ? ORDER BY device_id, sequence
    ''', (shift_id,))
    return tuple(_receipt_from_row(row) for row in rows)
