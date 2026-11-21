import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

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
from shift.opening import open_shift


@dataclass(frozen=True)
class DayResult:
    grades: tuple[GradeResult, ...]
    handover: ReportReadback


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
                result = run_day(Path(temporary))
                payload = asdict(result)
                for output, device in zip(payload['handover']['record']['report']['devices'], result.handover.record.report.devices):
                    output['recheck_receipts'] = [entry.receipt_id for entry in device.rechecks]
                print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (LookupError, ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f'应变记录站运行失败 {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
