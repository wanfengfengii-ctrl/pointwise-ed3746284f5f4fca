import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.custody import issue_ticket, read_ticket
from archive.freeze import seal
from archive.readback import ReportReadback, retrieve
from intake.accept import take_one
from intake.ledger import StationLedger
from intake.validation import TakeOneRequest
from quality.grading import GradeResult, grade
from recorder.capture import capture
from recorder.endmark import stop
from recorder.journal import DeviceJournal
from recorder.queue import peek
from recorder.retention import configure_capacity, reclaim, usage
from shift.opening import open_shift


@dataclass(frozen=True)
class DayResult:
    grades: tuple[GradeResult, ...]
    handover: ReportReadback


@dataclass(frozen=True)
class CustodyResult:
    capacity: int
    usage_after_release: int
    ticket_one: dict
    ticket_two: dict
    reclaim_one: dict
    reclaim_two: dict
    usage_after_reclaim: int
    offline_readback: ReportReadback
    replayed_action: str
    new_packet_sequence: int
    usage_with_new_packet: int
    new_shift_report: int


def initialize_workspace(directory: Path) -> tuple[StationLedger, dict[str, DeviceJournal]]:
    directory.mkdir(parents=True, exist_ok=True)
    ledger = StationLedger(directory / 'station.sqlite3')
    sources = {
        'L': DeviceJournal(directory / 'left.sqlite3', 'L'),
        'R': DeviceJournal(directory / 'right.sqlite3', 'R'),
    }
    ledger.initialize()
    for journal in sources.values():
        journal.initialize()
    return ledger, sources


def run_day(directory: Path) -> DayResult:
    ledger, sources = initialize_workspace(directory)
    protocol = open_shift(ledger, 'strain-day-01', ('L', 'R'), 'gauge-A', 8)
    shift_id = protocol.shift_id
    sample_dir = Path(__file__).resolve().parent / 'samples'
    left_samples = json.loads((sample_dir / 'left.json').read_text(encoding='utf-8'))
    right_samples = json.loads((sample_dir / 'right.json').read_text(encoding='utf-8'))

    def record_sample(device_id, row):
        return capture(ledger, sources[device_id], shift_id, row['action_id'], row['samples'], row['source'])

    left_first = record_sample('L', left_samples[0])
    right_first = record_sample('R', right_samples[0])
    preview = peek(ledger, sources['L'], shift_id)
    left_tail = [record_sample('L', row) for row in left_samples[1:]]
    if preview != left_first or peek(ledger, sources['L'], shift_id) != preview:
        raise RuntimeError('追加后左台预览与原队首不一致')

    grades = []
    for packet in (left_first, *left_tail[:-1], right_first):
        identity = packet.wave_id
        request = TakeOneRequest(f'intake-{packet.action_id}', shift_id, identity.device_id, identity)
        receipt = take_one(ledger, sources[identity.device_id], request)
        grades.append(grade(ledger, receipt.receipt_id))

    marks = [stop(ledger, sources[device_id], shift_id) for device_id in reversed(protocol.roster.devices)]
    archive = seal(ledger, sources, shift_id, marks)
    for journal in sources.values():
        journal.path.rename(journal.path.with_suffix('.sqlite3.offline'))
    handover = retrieve(StationLedger(ledger.path), archive.report_id)
    if handover is None:
        raise RuntimeError('断源重开后找不到已封班报')
    return DayResult(tuple(grades), handover)


def run_custody_handover(directory: Path) -> CustodyResult:
    """Issue fixed-report tickets, reclaim device bodies offline, then collect new work."""
    directory.mkdir(parents=True, exist_ok=True)
    ledger = StationLedger(directory / 'station.sqlite3')
    left_journal = DeviceJournal(directory / 'left.sqlite3', 'L')
    right_journal = DeviceJournal(directory / 'right.sqlite3', 'R')
    ledger.initialize()
    left_journal.initialize()
    right_journal.initialize()
    shift_id = 'custody-01'
    open_shift(ledger, shift_id, ('L', 'R'), 'gauge-A', 8)

    l1 = capture(ledger, left_journal, shift_id, 'L1', [-8, 8], '试片B/左规/首载')
    l2 = capture(ledger, left_journal, shift_id, 'L2', [-9, 3, 1, 2], '试片B/左规/加荷')
    capture(ledger, right_journal, shift_id, 'R1', [5, -2], '试片B/右规/首载')

    # L1 holds 2 points, L2 holds 4 points: the whole left device occupies 6.
    configure_capacity(left_journal, 6)
    receipt_one = take_one(
        ledger, left_journal,
        TakeOneRequest('intake-L1', shift_id, 'L', l1.wave_id))
    receipt_two = take_one(
        ledger, left_journal,
        TakeOneRequest('intake-L2', shift_id, 'L', l2.wave_id))
    usage_after_release = usage(left_journal)

    marks = [stop(ledger, left_journal, shift_id), stop(ledger, right_journal, shift_id)]
    archive = seal(ledger, {'L': left_journal, 'R': right_journal}, shift_id, marks)

    # Station persists re-issuable tickets before the device goes offline.
    ticket_one = issue_ticket(ledger, 'T1', archive.report_id, 'L', [receipt_one.receipt_id])
    ticket_two = issue_ticket(
        ledger, 'T2', archive.report_id, 'L',
        [receipt_one.receipt_id, receipt_two.receipt_id])
    if read_ticket(ledger, 'T1') != ticket_one:
        raise RuntimeError('保全票 T1 持久化后无法按号读回')

    # Device is disconnected from the station but keeps its own journal.
    reclaim_one = reclaim(left_journal, ticket_one)
    if reclaim_one['reclaimed_samples'] != 2:
        raise RuntimeError('T1 应收回 L1 的 2 个样点')
    reclaim_two = reclaim(left_journal, ticket_two)
    if reclaim_two['reclaimed_samples'] != 4:
        raise RuntimeError('T2 应再收回 L2 的 4 个样点')
    if reclaim(left_journal, ticket_one) != reclaim_one:
        raise RuntimeError('同票重投必须复领原处理结果')
    usage_after_reclaim = usage(left_journal)

    # Night shift: reopen the station alone and verify originals purely from station custody.
    left_offline = left_journal.path.with_suffix('.sqlite3.offline')
    right_offline = right_journal.path.with_suffix('.sqlite3.offline')
    left_journal.path.rename(left_offline)
    right_journal.path.rename(right_offline)
    offline_readback = retrieve(StationLedger(ledger.path), archive.report_id)
    if offline_readback is None:
        raise RuntimeError('断源后无法按固定班报核回原件')
    for trace in offline_readback.traces:
        if trace.packet.wave_id.device_id != 'L':
            continue
        persisted = next(member for ticket in (ticket_one, ticket_two)
                         for member in ticket['members']
                         if member['receipt_id'] == trace.receipt.receipt_id)
        if len(trace.packet.samples) != persisted['sample_count']:
            raise RuntimeError('站库核回样点长度与保全票不符')

    # Device returns. Replaying an old reclaimed action restores the original identity
    # without taking capacity or queue position.
    left_offline.rename(left_journal.path)
    right_offline.rename(right_journal.path)
    reconnected = DeviceJournal(left_journal.path, 'L')
    replayed = capture(ledger, reconnected, shift_id, 'L1',
                       [-8, 8], '试片B/左规/首载')
    if replayed != l1 or usage(reconnected) != 0:
        raise RuntimeError('旧动作重投须复领原身份且不重新占容量')
    if peek(ledger, reconnected, shift_id) is not None:
        raise RuntimeError('已回收旧件不能因重投回到队首')

    # New work is collected into the reclaimed space under a new frozen shift.
    next_shift = 'custody-02'
    open_shift(ledger, next_shift, ('L', 'R'), 'gauge-A', 8)
    new_packet = capture(ledger, reconnected, next_shift, 'N1', [3, -3], '试片C/左规/新载')
    new_marks = [stop(ledger, reconnected, next_shift),
                 stop(ledger, right_journal, next_shift)]
    new_archive = seal(ledger, {'L': reconnected, 'R': right_journal},
                       next_shift, new_marks)
    # The old fixed report is still readable unchanged.
    if retrieve(ledger, archive.report_id) != offline_readback:
        raise RuntimeError('新班次采集不能改变旧固定班报')
    return CustodyResult(
        capacity=6,
        usage_after_release=usage_after_release,
        ticket_one=ticket_one,
        ticket_two=ticket_two,
        reclaim_one=reclaim_one,
        reclaim_two=reclaim_two,
        usage_after_reclaim=usage_after_reclaim,
        offline_readback=offline_readback,
        replayed_action=replayed.action_id,
        new_packet_sequence=new_packet.wave_id.sequence,
        usage_with_new_packet=usage(reconnected),
        new_shift_report=new_archive.report_id,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='应变记录站初始化及白班到夜班演示')
    commands = parser.add_subparsers(dest='command', required=True)
    initialize = commands.add_parser('init', help='初始化工作目录')
    initialize.add_argument('directory', type=Path)
    commands.add_parser('demo', help='在临时库中执行完整日剧本')
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'init':
            ledger, sources = initialize_workspace(arguments.directory)
            print(json.dumps({'station': ledger.path.name, 'sources': [j.path.name for j in sources.values()]}))
        else:
            with TemporaryDirectory(prefix='strain-station-') as temporary:
                root = Path(temporary)
                result = run_day(root / 'day')
                custody = run_custody_handover(root / 'custody')
                payload = {'day': asdict(result), 'custody': asdict(custody)}
                for output, device in zip(
                        payload['day']['handover']['record']['report']['devices'],
                        result.handover.record.report.devices):
                    output['recheck_receipts'] = [entry.receipt_id for entry in device.rechecks]
                custody_payload = payload['custody']
                custody_payload['offline_readback'] = asdict(custody.offline_readback)
                custody_payload['reclaim_order'] = {
                    'T1': custody.reclaim_one['reclaimed_samples'],
                    'T2': custody.reclaim_two['reclaimed_samples'],
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (LookupError, ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f'应变记录站运行失败 {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
