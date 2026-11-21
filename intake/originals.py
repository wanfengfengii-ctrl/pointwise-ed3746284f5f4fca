import sqlite3

from recorder.packet import WaveId, WavePacket, decode_packet, encode_packet


def store_original(connection: sqlite3.Connection, packet: WavePacket) -> None:
    identity = packet.wave_id
    connection.execute('''
        INSERT INTO originals (shift_id, device_id, sequence, packet) VALUES (?, ?, ?, ?)
    ''', (identity.shift_id, identity.device_id, identity.sequence, encode_packet(packet)))


def load_original(connection: sqlite3.Connection, identity: WaveId) -> WavePacket | None:
    row = connection.execute('''
        SELECT packet FROM originals WHERE shift_id = ? AND device_id = ? AND sequence = ?
    ''', (identity.shift_id, identity.device_id, identity.sequence)).fetchone()
    if row is None:
        return None
    try:
        packet = decode_packet(row['packet'])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'站库原件无法读取 {identity!r}') from error
    if packet.wave_id != identity:
        raise RuntimeError(f'站库原件身份与索引不符 {identity!r}')
    return packet
