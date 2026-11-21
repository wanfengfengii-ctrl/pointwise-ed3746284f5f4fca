import json

from archive.catalog import load_report
from intake.ledger import StationLedger
from intake.originals import load_original
from intake.receipts import load_receipt
from recorder.packet import packet_digest
from shift.opening import load_protocol


def _require_name(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}须为非空文本')


def _canonical(ticket: dict) -> str:
    return json.dumps(ticket, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def issue_ticket(ledger: StationLedger, ticket_id, report_id, device_id,
                 receipt_ids) -> dict:
    """Persist a re-issuable custody ticket at the trusted station ledger."""
    _require_name(ticket_id, '票号')
    _require_name(device_id, '设备号')
    if type(report_id) is not int or not 1 <= report_id <= (1 << 63) - 1:
        raise ValueError('班报号无效')
    if (not isinstance(receipt_ids, (list, tuple)) or not receipt_ids
            or any(type(receipt_id) is not int for receipt_id in receipt_ids)):
        raise ValueError('首执号须为非空整数清单')
    if any(receipt_ids[index] >= receipt_ids[index + 1]
           for index in range(len(receipt_ids) - 1)):
        raise ValueError('首执号须非空且严格递增')

    with ledger.write_transaction() as connection:
        existing = connection.execute(
            'SELECT payload FROM custody_tickets WHERE ticket_id = ?', (ticket_id,),
        ).fetchone()

        record = load_report(connection, report_id)
        if record is None:
            raise ValueError(f'未知固定班报 {report_id}')
        report = record.report
        shift_id = report.shift_id
        protocol = load_protocol(connection, shift_id)
        protocol.roster.require_device(device_id)
        device_report = next(
            (device for device in report.devices
             if device.endmark.device_id == device_id), None)
        if device_report is None:
            raise ValueError(f'班报 {report_id} 不含设备 {device_id!r}')
        report_entries = {entry.receipt_id: entry for entry in device_report.entries}

        members = []
        for receipt_id in receipt_ids:
            if receipt_id not in report_entries:
                raise ValueError(
                    f'首执 {receipt_id} 不在固定班报 {report_id} 的设备 {device_id!r} 清册内')
            receipt = load_receipt(connection, receipt_id)
            if receipt is None:
                raise RuntimeError(f'班报条目引用的首执 {receipt_id} 缺失')
            identity = report_entries[receipt_id].wave_id
            if receipt.wave_id != identity:
                raise RuntimeError(f'首执 {receipt_id} 与班报原件身份不符')
            packet = load_original(connection, identity)
            if packet is None:
                raise RuntimeError(f'首执 {receipt_id} 的站库全量原件缺失')
            if (packet.wave_id.shift_id, packet.wave_id.device_id) != (shift_id, device_id):
                raise RuntimeError(f'首执 {receipt_id} 的原件出处与票头不符')
            members.append({
                'receipt_id': receipt_id,
                'wave_id': {
                    'shift_id': identity.shift_id,
                    'device_id': identity.device_id,
                    'sequence': identity.sequence,
                },
                'sample_count': len(packet.samples),
                'body_digest': packet_digest(packet),
            })

        ticket = {
            'ticket_id': ticket_id,
            'report_id': report_id,
            'shift_id': shift_id,
            'device_id': device_id,
            'members': members,
        }
        payload = _canonical(ticket)
        if existing is not None:
            if existing['payload'] != payload:
                raise ValueError(f'票号 {ticket_id!r} 已按另一组参数签发')
            return ticket
        connection.execute('''
            INSERT INTO custody_tickets (ticket_id, report_id, payload)
            VALUES (?, ?, ?)
        ''', (ticket_id, report_id, payload))
        return ticket


def read_ticket(ledger: StationLedger, ticket_id) -> dict | None:
    _require_name(ticket_id, '票号')
    with ledger.read_transaction() as connection:
        row = connection.execute(
            'SELECT payload FROM custody_tickets WHERE ticket_id = ?', (ticket_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            ticket = json.loads(row['payload'])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f'保全票 {ticket_id!r} 的持久内容无效') from error
        if not isinstance(ticket, dict) or ticket.get('ticket_id') != ticket_id:
            raise RuntimeError(f'保全票 {ticket_id!r} 的持久身份不符')
        return ticket
