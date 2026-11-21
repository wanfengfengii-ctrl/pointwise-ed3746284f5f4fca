from intake.ledger import StationLedger
from recorder.endmark import read_endmark
from recorder.journal import DeviceJournal
from recorder.packet import WavePacket, capture_payload_matches, validate_capture_payload
from shift.opening import load_protocol


def capture(ledger: StationLedger, journal: DeviceJournal, shift_id: str,
            action_id: str, samples, source: str) -> WavePacket:
    values = validate_capture_payload(action_id, samples, source)
    with ledger.read_transaction() as station:
        protocol = load_protocol(station, shift_id)
        protocol.roster.require_device(journal.device_id)

    with journal.transaction() as device:
        previous = journal.find_capture(device, shift_id, action_id)
        if previous is not None:
            if not capture_payload_matches(previous, action_id, values, source):
                raise ValueError(f'采集动作 {action_id!r} 的波形或出处冲突')
            return previous
        if read_endmark(journal, device, shift_id) is not None:
            raise ValueError(f'设备 {journal.device_id!r} 已在本班停采')
        return journal.append_packet(device, shift_id, action_id, values, source)
