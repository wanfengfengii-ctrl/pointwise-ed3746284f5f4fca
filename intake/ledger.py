import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class StationLedger:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def _connect(self, mode: str) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f'{self.path.as_uri()}?mode={mode}', uri=True, isolation_level=None, timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return connection

    def initialize(self) -> None:
        from archive.catalog import initialize_archive_schema
        from shift.opening import initialize_shift_schema

        with closing(self._connect('rwc')) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            initialize_shift_schema(connection)
            connection.execute('''
                CREATE TABLE IF NOT EXISTS originals (
                    shift_id TEXT NOT NULL REFERENCES shifts (shift_id),
                    device_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    packet TEXT NOT NULL,
                    PRIMARY KEY (shift_id, device_id, sequence)
                )
            ''')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    shift_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    UNIQUE (shift_id, device_id, sequence),
                    FOREIGN KEY (shift_id, device_id, sequence)
                        REFERENCES originals (shift_id, device_id, sequence)
                )
            ''')
            initialize_archive_schema(connection)

    @contextmanager
    def write_transaction(self):
        with closing(self._connect('rw')) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection

    @contextmanager
    def read_transaction(self):
        with closing(self._connect('ro')) as connection, connection:
            connection.execute('BEGIN')
            yield connection
