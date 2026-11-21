import sqlite3

from intake.ledger import StationLedger
from shift.protocol import ShiftProtocol, build_protocol
from shift.roster import DeviceRoster


def initialize_shift_schema(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            shift_id TEXT PRIMARY KEY NOT NULL,
            left_device TEXT NOT NULL,
            right_device TEXT NOT NULL,
            channel TEXT NOT NULL,
            threshold TEXT NOT NULL,
            unit TEXT NOT NULL,
            CHECK (left_device <> right_device)
        )
    ''')


def _find_protocol(connection, shift_id) -> ShiftProtocol | None:
    if not isinstance(shift_id, str) or not shift_id.strip():
        raise ValueError('班号须为非空文本')
    row = connection.execute('SELECT * FROM shifts WHERE shift_id = ?', (shift_id,)).fetchone()
    if row is None:
        return None
    try:
        return ShiftProtocol(
            row['shift_id'], DeviceRoster(row['left_device'], row['right_device']),
            row['channel'], int(row['threshold'], 16), row['unit'],
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError(f'班次 {shift_id!r} 的持久规程无效') from error


def load_protocol(connection: sqlite3.Connection, shift_id: str) -> ShiftProtocol:
    protocol = _find_protocol(connection, shift_id)
    if protocol is None:
        raise LookupError(f'未知班次 {shift_id!r}')
    return protocol


def open_shift(ledger: StationLedger, shift_id, devices, channel, threshold) -> ShiftProtocol:
    requested = build_protocol(shift_id, devices, channel, threshold)
    with ledger.write_transaction() as connection:
        existing = _find_protocol(connection, shift_id)
        if existing is not None:
            if existing != requested:
                raise ValueError(f'班次 {shift_id!r} 已冻结为另一规程')
            return existing
        connection.execute('''
            INSERT INTO shifts (shift_id, left_device, right_device, channel, threshold, unit)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (shift_id, *requested.roster.devices, channel, hex(threshold), requested.unit))
    return requested
