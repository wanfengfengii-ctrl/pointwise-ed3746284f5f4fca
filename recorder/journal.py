import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from recorder.packet import (
    WaveId, WavePacket, decode_packet, encode_packet, encode_tombstone,
    make_packet, packet_digest,
)


@dataclass(frozen=True)
class DeviceJournal:
    path: Path
    device_id: str

    def __post_init__(self):
        object.__setattr__(self, 'path', Path(self.path).absolute())
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise ValueError('设备号须为非空文本')

    def _connect(self, mode: str) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f'{self.path.as_uri()}?mode={mode}', uri=True, isolation_level=None, timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return connection

    def _require_identity(self, connection) -> None:
        row = connection.execute('SELECT device_id FROM source_identity WHERE slot = 1').fetchone()
        if row is None or row['device_id'] != self.device_id:
            raise ValueError(f'源库与设备 {self.device_id!r} 的绑定不符')

    def initialize(self) -> None:
        with closing(self._connect('rwc')) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS source_identity (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1), device_id TEXT NOT NULL
                )
            ''')
            connection.execute('''
                INSERT INTO source_identity (slot, device_id) VALUES (1, ?)
                ON CONFLICT(slot) DO NOTHING
            ''', (self.device_id,))
            self._require_identity(connection)
            connection.execute('''
                CREATE TABLE IF NOT EXISTS captures (
                    shift_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    action_id TEXT NOT NULL,
                    packet TEXT NOT NULL,
                    body_cleared INTEGER NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    body_digest TEXT,
                    PRIMARY KEY (shift_id, sequence),
                    UNIQUE (shift_id, action_id)
                )
            ''')
            capture_columns = {
                row['name'] for row in connection.execute('PRAGMA table_info(captures)')
            }
            if 'body_cleared' not in capture_columns:
                connection.execute('ALTER TABLE captures ADD COLUMN body_cleared INTEGER NOT NULL DEFAULT 0')
            if 'sample_count' not in capture_columns:
                connection.execute('ALTER TABLE captures ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 0')
            if 'body_digest' not in capture_columns:
                connection.execute('ALTER TABLE captures ADD COLUMN body_digest TEXT')
            legacy_rows = connection.execute('''
                SELECT shift_id, sequence, packet FROM captures
                WHERE sample_count = 0 AND body_cleared = 0
            ''').fetchall()
            for row in legacy_rows:
                packet = decode_packet(row['packet'])
                connection.execute('''
                    UPDATE captures
                    SET sample_count = ?, body_digest = ?
                    WHERE shift_id = ? AND sequence = ?
                ''', (len(packet.samples), packet_digest(packet), row['shift_id'], row['sequence']))
            connection.execute('''
                CREATE TABLE IF NOT EXISTS capacity_limits (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    max_samples INTEGER NOT NULL CHECK (max_samples > 0)
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS reclaims (
                    ticket_id TEXT PRIMARY KEY NOT NULL,
                    report_id INTEGER NOT NULL,
                    reclaimed_samples INTEGER NOT NULL CHECK (reclaimed_samples >= 0),
                    payload TEXT NOT NULL
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS reclaim_members (
                    ticket_id TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    receipt_id INTEGER NOT NULL,
                    PRIMARY KEY (ticket_id, shift_id, sequence),
                    FOREIGN KEY (shift_id, sequence) REFERENCES captures (shift_id, sequence)
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS releases (
                    shift_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    receipt_id INTEGER NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    PRIMARY KEY (shift_id, sequence),
                    FOREIGN KEY (shift_id, sequence) REFERENCES captures (shift_id, sequence)
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS endmarks (
                    shift_id TEXT PRIMARY KEY NOT NULL,
                    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0)
                )
            ''')

    @contextmanager
    def transaction(self, *, write: bool = True):
        """Open a local source transaction, closed when the caller leaves the block."""
        mode = 'rw' if write else 'ro'
        with closing(self._connect(mode)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            self._require_identity(connection)
            yield connection

    def find_capture(self, connection, shift_id: str, action_id: str) -> WavePacket | None:
        row = connection.execute('''
            SELECT sequence, body_cleared FROM captures
            WHERE shift_id = ? AND action_id = ?
        ''', (shift_id, action_id)).fetchone()
        if row is None:
            return None
        if row['body_cleared']:
            return None
        return self.load_packet(connection, WaveId(shift_id, self.device_id, row['sequence']))

    def find_capture_row(self, connection, shift_id: str, action_id: str):
        return connection.execute('''
            SELECT * FROM captures WHERE shift_id = ? AND action_id = ?
        ''', (shift_id, action_id)).fetchone()

    def find_capture_by_sequence(self, connection, shift_id: str, sequence: int):
        return connection.execute('''
            SELECT * FROM captures WHERE shift_id = ? AND sequence = ?
        ''', (shift_id, sequence)).fetchone()

    def last_sequence(self, connection, shift_id: str) -> int:
        return connection.execute('''
            SELECT COALESCE(MAX(sequence), 0) FROM captures WHERE shift_id = ?
        ''', (shift_id,)).fetchone()[0]

    def append_packet(self, connection, shift_id, action_id, samples, source) -> WavePacket:
        sequence = self.last_sequence(connection, shift_id) + 1
        packet = make_packet(shift_id, self.device_id, sequence, action_id, samples, source)
        connection.execute('''
            INSERT INTO captures
                (shift_id, sequence, action_id, packet, body_cleared, sample_count, body_digest)
            VALUES (?, ?, ?, ?, 0, ?, ?)
        ''', (shift_id, sequence, action_id, encode_packet(packet),
              len(packet.samples), packet_digest(packet)))
        return packet

    def load_packet(self, connection, wave_id: WaveId) -> WavePacket | None:
        if wave_id.device_id != self.device_id:
            raise ValueError('设备与原件身份不符')
        row = connection.execute('''
            SELECT action_id, packet, body_cleared FROM captures
            WHERE shift_id = ? AND sequence = ?
        ''', (wave_id.shift_id, wave_id.sequence)).fetchone()
        if row is None:
            return None
        if row['body_cleared']:
            raise RuntimeError(f'原件正文已回收 {wave_id!r}')
        try:
            packet = decode_packet(row['packet'])
        except (TypeError, ValueError, KeyError) as error:
            raise RuntimeError(f'原包无法读取 {wave_id!r}') from error
        if packet.wave_id != wave_id or packet.action_id != row['action_id']:
            raise RuntimeError(f'原包与源库索引不符 {wave_id!r}')
        return packet

    def clear_capture_body(self, connection, shift_id: str, sequence: int,
                           packet: WavePacket) -> None:
        connection.execute('''
            UPDATE captures
            SET packet = ?, body_cleared = 1, sample_count = ?, body_digest = ?
            WHERE shift_id = ? AND sequence = ? AND body_cleared = 0
        ''', (encode_tombstone(packet), len(packet.samples), packet_digest(packet),
              shift_id, sequence))

    def current_usage(self, connection) -> int:
        """Total live sample points still held on this device across shifts."""
        row = connection.execute('''
            SELECT COALESCE(SUM(sample_count), 0) AS used
            FROM captures WHERE body_cleared = 0
        ''').fetchone()
        return int(row['used'])

    def read_capacity_limit(self, connection) -> int | None:
        row = connection.execute('SELECT max_samples FROM capacity_limits WHERE slot = 1').fetchone()
        return None if row is None else int(row['max_samples'])

    def write_capacity_limit(self, connection, max_samples: int) -> None:
        connection.execute('''
            INSERT INTO capacity_limits (slot, max_samples) VALUES (1, ?)
            ON CONFLICT(slot) DO UPDATE SET max_samples = excluded.max_samples
        ''', (max_samples,))

    def find_reclaim(self, connection, ticket_id: str):
        return connection.execute('''
            SELECT * FROM reclaims WHERE ticket_id = ?
        ''', (ticket_id,)).fetchone()

    def insert_reclaim(self, connection, ticket_id: str, report_id: int,
                       reclaimed_samples: int, payload: str) -> None:
        connection.execute('''
            INSERT INTO reclaims (ticket_id, report_id, reclaimed_samples, payload)
            VALUES (?, ?, ?, ?)
        ''', (ticket_id, report_id, reclaimed_samples, payload))

    def insert_reclaim_member(self, connection, ticket_id: str, shift_id: str,
                              sequence: int, receipt_id: int) -> None:
        connection.execute('''
            INSERT INTO reclaim_members (ticket_id, shift_id, sequence, receipt_id)
            VALUES (?, ?, ?, ?)
        ''', (ticket_id, shift_id, sequence, receipt_id))

    def member_ticket(self, connection, shift_id: str, sequence: int):
        return connection.execute('''
            SELECT ticket_id FROM reclaim_members WHERE shift_id = ? AND sequence = ?
        ''', (shift_id, sequence)).fetchone()
