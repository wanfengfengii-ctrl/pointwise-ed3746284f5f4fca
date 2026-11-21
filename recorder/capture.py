import json

from intake.ledger import StationLedger
from recorder.endmark import read_endmark
from recorder.journal import DeviceJournal
from recorder.packet import (
    WaveId, WavePacket, capture_payload_matches, decode_packet, decode_tombstone,
    make_packet, packet_digest, validate_capture_payload,
)
from shift.opening import load_protocol


def _tombstone_matches(row, action_id, values, source) -> bool:
    """Verify an exact replay against a capture whose waveform body was reclaimed."""
    try:
        marker = decode_tombstone(row['packet'])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if row['action_id'] != action_id or marker['action_id'] != action_id:
        return False
    if marker['source'] != source or row['sample_count'] != len(values):
        return False
    candidate = make_packet(marker['shift_id'], marker['device_id'], row['sequence'],
                            action_id, values, source)
    return packet_digest(candidate) == row['body_digest'] == marker['body_digest']


def capture(ledger: StationLedger, journal: DeviceJournal, shift_id: str,
            action_id: str, samples, source: str) -> WavePacket:
    values = validate_capture_payload(action_id, samples, source)
    with ledger.read_transaction() as station:
        protocol = load_protocol(station, shift_id)
        protocol.roster.require_device(journal.device_id)

    with journal.transaction() as device:
        row = journal.find_capture_row(device, shift_id, action_id)
        if row is not None:
            identity = WaveId(shift_id, journal.device_id, row['sequence'])
            if row['body_cleared']:
                if not _tombstone_matches(row, action_id, values, source):
                    raise ValueError(f'采集动作 {action_id!r} 的波形或出处冲突')
                # Reclaimed identity replay: return the equivalent WavePacket without
                # taking new capacity, a new source sequence or a queue slot.
                return make_packet(shift_id, journal.device_id, row['sequence'],
                                   action_id, values, source)
            previous = journal.load_packet(device, identity)
            if not capture_payload_matches(previous, action_id, values, source):
                raise ValueError(f'采集动作 {action_id!r} 的波形或出处冲突')
            return previous
        if read_endmark(journal, device, shift_id) is not None:
            raise ValueError(f'设备 {journal.device_id!r} 已在本班停采')
        limit = journal.read_capacity_limit(device)
        if limit is not None:
            used = journal.current_usage(device)
            if used + len(values) > limit:
                raise ValueError(
                    f'设备 {journal.device_id!r} 容量不足：本机留存 {used} 点，'
                    f'新件 {len(values)} 点，额度 {limit}')
        return journal.append_packet(device, shift_id, action_id, values, source)
