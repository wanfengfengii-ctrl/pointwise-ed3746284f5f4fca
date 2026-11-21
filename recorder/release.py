from dataclasses import dataclass

from intake.ledger import StationLedger
from intake.originals import load_original
from intake.receipts import load_receipt
from recorder.journal import DeviceJournal
from recorder.packet import WaveId, WavePacket
from recorder.queue import read_current_head


@dataclass(frozen=True)
class ReleaseEvidence:
    receipt_id: int
    request_id: str
    wave_id: WaveId


def _read_committed_original(ledger: StationLedger, evidence: ReleaseEvidence) -> WavePacket:
    with ledger.read_transaction() as station:
        receipt = load_receipt(station, evidence.receipt_id)
        if receipt is None or (receipt.request_id, receipt.wave_id) != (evidence.request_id, evidence.wave_id):
            raise ValueError('释放依据与已提交的普通首执不符')
        original = load_original(station, receipt.wave_id)
        if original is None:
            raise RuntimeError('普通首执所对应的站库原件缺失')
        return original


def release_head(ledger: StationLedger, journal: DeviceJournal,
                 evidence: ReleaseEvidence) -> ReleaseEvidence:
    if not isinstance(evidence.wave_id, WaveId) or evidence.wave_id.device_id != journal.device_id:
        raise ValueError('释放依据与本台设备不符')
    committed = _read_committed_original(ledger, evidence)
    identity = evidence.wave_id
    with journal.transaction() as device:
        previous = device.execute('''
            SELECT receipt_id, request_id FROM releases WHERE shift_id = ? AND sequence = ?
        ''', (identity.shift_id, identity.sequence)).fetchone()
        if previous is not None:
            if (previous['receipt_id'], previous['request_id']) != (evidence.receipt_id, evidence.request_id):
                raise ValueError('原件已用另一份凭据释放')
            return evidence

        head = read_current_head(journal, device, identity.shift_id)
        if head is None or head.wave_id != identity:
            raise ValueError('释放目标已非确切队首')
        if head != committed:
            raise RuntimeError('待释放原包与站库正式原件不符')
        device.execute('''
            INSERT INTO releases (shift_id, sequence, receipt_id, request_id) VALUES (?, ?, ?, ?)
        ''', (identity.shift_id, identity.sequence, evidence.receipt_id, evidence.request_id))
    return evidence
