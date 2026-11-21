import sqlite3

from intake.ledger import StationLedger
from recorder.journal import DeviceJournal
from recorder.packet import WaveId, WavePacket
from shift.opening import load_protocol


def read_current_head(journal: DeviceJournal, connection: sqlite3.Connection,
                      shift_id: str) -> WavePacket | None:
    row = connection.execute('''
        SELECT capture.sequence
        FROM captures AS capture
        LEFT JOIN releases AS released
            ON released.shift_id = capture.shift_id AND released.sequence = capture.sequence
        WHERE capture.shift_id = ? AND released.sequence IS NULL
        ORDER BY capture.sequence
        LIMIT 1
    ''', (shift_id,)).fetchone()
    if row is None:
        return None
    return journal.load_packet(connection, WaveId(shift_id, journal.device_id, row['sequence']))


def peek(ledger: StationLedger, journal: DeviceJournal, shift_id: str) -> WavePacket | None:
    with ledger.read_transaction() as station:
        protocol = load_protocol(station, shift_id)
        protocol.roster.require_device(journal.device_id)
    with journal.transaction(write=False) as device:
        return read_current_head(journal, device, shift_id)
