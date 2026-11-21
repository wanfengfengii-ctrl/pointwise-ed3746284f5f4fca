import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from recorder.packet import WaveId, WavePacket, decode_packet, encode_packet, make_packet


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
                    PRIMARY KEY (shift_id, sequence),
                    UNIQUE (shift_id, action_id)
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
            SELECT sequence FROM captures WHERE shift_id = ? AND action_id = ?
        ''', (shift_id, action_id)).fetchone()
        if row is None:
            return None
        return self.load_packet(connection, WaveId(shift_id, self.device_id, row['sequence']))

    def last_sequence(self, connection, shift_id: str) -> int:
        return connection.execute('''
            SELECT COALESCE(MAX(sequence), 0) FROM captures WHERE shift_id = ?
        ''', (shift_id,)).fetchone()[0]

    def append_packet(self, connection, shift_id, action_id, samples, source) -> WavePacket:
        sequence = self.last_sequence(connection, shift_id) + 1
        packet = make_packet(shift_id, self.device_id, sequence, action_id, samples, source)
        connection.execute('''
            INSERT INTO captures (shift_id, sequence, action_id, packet) VALUES (?, ?, ?, ?)
        ''', (shift_id, sequence, action_id, encode_packet(packet)))
        return packet

    def load_packet(self, connection, wave_id: WaveId) -> WavePacket | None:
        if wave_id.device_id != self.device_id:
            raise ValueError('设备与原件身份不符')
        row = connection.execute('''
            SELECT action_id, packet FROM captures WHERE shift_id = ? AND sequence = ?
        ''', (wave_id.shift_id, wave_id.sequence)).fetchone()
        if row is None:
            return None
        try:
            packet = decode_packet(row['packet'])
        except (TypeError, ValueError, KeyError) as error:
            raise RuntimeError(f'原包无法读取 {wave_id!r}') from error
        if packet.wave_id != wave_id or packet.action_id != row['action_id']:
            raise RuntimeError(f'原包与源库索引不符 {wave_id!r}')
        return packet
