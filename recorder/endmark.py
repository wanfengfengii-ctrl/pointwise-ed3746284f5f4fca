from dataclasses import dataclass

from intake.ledger import StationLedger
from recorder.journal import DeviceJournal
from recorder.packet import MAX_SOURCE_SEQUENCE
from shift.opening import load_protocol


@dataclass(frozen=True)
class EndMark:
    shift_id: str
    device_id: str
    last_sequence: int

    def __post_init__(self):
        if any(not isinstance(name, str) or not name.strip() for name in (self.shift_id, self.device_id)):
            raise ValueError('末序须绑定班号及设备号')
        if type(self.last_sequence) is not int or not 0 <= self.last_sequence <= MAX_SOURCE_SEQUENCE:
            raise ValueError('设备末序无效')


def read_endmark(journal: DeviceJournal, connection, shift_id: str) -> EndMark | None:
    row = connection.execute('SELECT last_sequence FROM endmarks WHERE shift_id = ?', (shift_id,)).fetchone()
    if row is None:
        return None
    return EndMark(shift_id, journal.device_id, row['last_sequence'])


def load_endmark(journal: DeviceJournal, shift_id: str) -> EndMark | None:
    with journal.transaction(write=False) as device:
        return read_endmark(journal, device, shift_id)


def stop(ledger: StationLedger, journal: DeviceJournal, shift_id: str) -> EndMark:
    with ledger.read_transaction() as station:
        protocol = load_protocol(station, shift_id)
        protocol.roster.require_device(journal.device_id)
    with journal.transaction() as device:
        previous = read_endmark(journal, device, shift_id)
        if previous is not None:
            return previous
        mark = EndMark(shift_id, journal.device_id, journal.last_sequence(device, shift_id))
        device.execute('''
            INSERT INTO endmarks (shift_id, last_sequence) VALUES (?, ?)
        ''', (mark.shift_id, mark.last_sequence))
        return mark
