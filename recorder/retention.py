import json
from collections.abc import Callable
from dataclasses import dataclass

from recorder.journal import DeviceJournal
from recorder.packet import WaveId, decode_packet, packet_digest


@dataclass(frozen=True)
class ReclaimResult:
    ticket_id: str
    report_id: int
    reclaimed_samples: int
    receipt_ids: tuple[int, ...]


def _require_name(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}须为非空文本')


def configure_capacity(journal: DeviceJournal, max_samples: int) -> None:
    """Register or change this device's cross-shift live sample-point quota."""
    if type(max_samples) is not int or max_samples <= 0:
        raise ValueError('容量额度须为正整数')
    with journal.transaction() as connection:
        used = journal.current_usage(connection)
        if used > max_samples:
            raise ValueError(
                f'容量额度 {max_samples} 小于本机当前留存 {used} 点')
        journal.write_capacity_limit(connection, max_samples)


def usage(journal: DeviceJournal) -> int:
    """Live sample points still occupying this device across all shifts."""
    with journal.transaction(write=False) as connection:
        return journal.current_usage(connection)


def _validate_ticket(ticket, journal: DeviceJournal) -> dict:
    if not isinstance(ticket, dict):
        raise ValueError('保全票须为 JSON 对象')
    ticket_id = ticket.get('ticket_id')
    report_id = ticket.get('report_id')
    shift_id = ticket.get('shift_id')
    device_id = ticket.get('device_id')
    members = ticket.get('members')
    _require_name(ticket_id, '票号')
    if type(report_id) is not int or not 1 <= report_id <= (1 << 63) - 1:
        raise ValueError('保全票的班报号无效')
    _require_name(shift_id, '班号')
    _require_name(device_id, '设备号')
    if device_id != journal.device_id:
        raise ValueError(f'保全票绑定设备 {device_id!r} 与本机 {journal.device_id!r} 不符')
    if not isinstance(members, list) or not members:
        raise ValueError('保全票须包含非空成员清单')

    receipt_ids = []
    seen_waves = set()
    checked = []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError('保全票成员须为对象')
        receipt_id = member.get('receipt_id')
        sample_count = member.get('sample_count')
        body_digest = member.get('body_digest')
        wave = member.get('wave_id')
        if type(receipt_id) is not int or not 1 <= receipt_id <= (1 << 63) - 1:
            raise ValueError('保全票成员的原首执号无效')
        if type(sample_count) is not int or sample_count <= 0:
            raise ValueError('保全票成员的样点长度无效')
        _require_name(body_digest, '正文摘要')
        if not isinstance(wave, dict):
            raise ValueError('保全票成员须写明原件身份')
        wave_shift = wave.get('shift_id')
        wave_device = wave.get('device_id')
        sequence = wave.get('sequence')
        _require_name(wave_shift, '原件班号')
        _require_name(wave_device, '原件设备号')
        if type(sequence) is not int or sequence <= 0:
            raise ValueError('原件源序无效')
        if wave_shift != shift_id or wave_device != device_id:
            raise ValueError('保全票成员的原件身份与票头绑定不符')
        if receipt_id in receipt_ids:
            raise ValueError(f'保全票含重复首执 {receipt_id}')
        receipt_ids.append(receipt_id)
        identity = (wave_shift, sequence)
        if identity in seen_waves:
            raise ValueError('保全票含重复原件成员')
        seen_waves.add(identity)
        checked.append({
            'receipt_id': receipt_id,
            'sample_count': sample_count,
            'body_digest': body_digest,
            'wave_id': WaveId(wave_shift, wave_device, sequence),
        })
    if receipt_ids != sorted(receipt_ids):
        raise ValueError('保全票的首执号须严格递增')
    return {
        'ticket_id': ticket_id,
        'report_id': report_id,
        'shift_id': shift_id,
        'device_id': device_id,
        'members': checked,
    }


def reclaim(journal: DeviceJournal, ticket, *,
            before_commit: Callable[[ReclaimResult], None] | None = None) -> dict:
    """Reclaim waveform bodies that this device has released and the station holds."""
    checked = _validate_ticket(ticket, journal)
    ticket_id = checked['ticket_id']
    shift_id = checked['shift_id']
    payload = json.dumps(ticket, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False)

    with journal.transaction() as connection:
        previous = journal.find_reclaim(connection, ticket_id)
        if previous is not None:
            if previous['payload'] != payload:
                raise ValueError(f'票号 {ticket_id!r} 已按另一组参数办理')
            return {
                'reclaimed_samples': int(previous['reclaimed_samples']),
                'receipt_ids': [member['receipt_id'] for member in checked['members']],
            }

        # Verify every member against this device's own release and original
        # records before any body is removed.
        plans = []
        reclaimed_samples = 0
        for member in checked['members']:
            identity = member['wave_id']
            row = journal.find_capture_by_sequence(connection, identity.shift_id,
                                                   identity.sequence)
            if row is None:
                raise ValueError(
                    f'本机没有首执 {member["receipt_id"]} 对应的原件 {identity!r}')
            release = connection.execute('''
                SELECT receipt_id FROM releases
                WHERE shift_id = ? AND sequence = ?
            ''', (identity.shift_id, identity.sequence)).fetchone()
            if release is None:
                raise ValueError(f'原件 {identity!r} 尚未确切放行，不能回收')
            if release['receipt_id'] != member['receipt_id']:
                raise ValueError(
                    f'首执 {member["receipt_id"]} 与本机放行凭据 {release["receipt_id"]} 错绑')
            if row['sample_count'] != member['sample_count']:
                raise ValueError(f'原件 {identity!r} 的样点长度与保全票不符')
            if row['body_digest'] != member['body_digest']:
                raise ValueError(f'原件 {identity!r} 的正文摘要与保全票不符')

            if row['body_cleared']:
                # Already reclaimed by another valid ticket: keep identity, skip body.
                continue
            try:
                packet = decode_packet(row['packet'])
            except (TypeError, ValueError, KeyError) as error:
                raise RuntimeError(f'待回收原包无法读取 {identity!r}') from error
            if packet_digest(packet) != member['body_digest'] or len(packet.samples) != member['sample_count']:
                raise ValueError(f'原件 {identity!r} 的正文与保全票摘要不符')
            plans.append((identity, packet))
            reclaimed_samples += member['sample_count']

        result = ReclaimResult(ticket_id, checked['report_id'], reclaimed_samples,
                               tuple(member['receipt_id'] for member in checked['members']))
        if before_commit is not None:
            before_commit(result)

        journal.insert_reclaim(connection, ticket_id, checked['report_id'],
                               reclaimed_samples, payload)
        for member in checked['members']:
            journal.insert_reclaim_member(
                connection, ticket_id, member['wave_id'].shift_id,
                member['wave_id'].sequence, member['receipt_id'])
        for identity, packet in plans:
            journal.clear_capture_body(connection, identity.shift_id,
                                       identity.sequence, packet)

    return {
        'reclaimed_samples': reclaimed_samples,
        'receipt_ids': list(result.receipt_ids),
    }
